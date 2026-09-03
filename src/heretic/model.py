# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026  Philipp Emanuel Weidmann <pew@worldwidemann.com> + contributors

import hashlib
import json
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Type, cast

import torch
from peft import LoraConfig, PeftModel, get_peft_model
from torch import FloatTensor, LongTensor, Tensor
from torch.nn import Module, ModuleList
from transformers import (
    AutoModelForCausalLM,
    AutoModelForImageTextToText,
    AutoProcessor,
    AutoTokenizer,
    BatchEncoding,
    BitsAndBytesConfig,
    PretrainedConfig,
    PreTrainedModel,
    PreTrainedTokenizerBase,
    ProcessorMixin,
    TextStreamer,
)
from transformers.generation import (
    GenerateDecoderOnlyOutput,  # ty:ignore[possibly-missing-import]
)

from . import ara
from .config import AbliterationMethod, QuantizationMethod, RowNormalization, Settings
from .system import empty_cache
from .targeting import discover_layer_modules
from .utils import Prompt, batchify, format_exception, print


def get_model_class(
    model: str,
    *,
    revision: str | None = None,
) -> Type[AutoModelForImageTextToText] | Type[AutoModelForCausalLM]:
    configs = PretrainedConfig.get_config_dict(model, revision=revision)

    if any([("vision_config" in config) for config in configs]):
        return AutoModelForImageTextToText
    else:
        return AutoModelForCausalLM


def merge_generation_kwargs(
    configured: dict[str, Any],
    callsite: dict[str, Any],
    reserved: dict[str, Any],
) -> dict[str, Any]:
    """Merge generation settings with explicit, deterministic precedence."""
    merged = {"do_sample": False, **configured, **callsite}
    merged.update(reserved)
    return merged


def _manifest_fingerprint(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _base_target_name(name: str) -> str:
    return name.removeprefix("base_model.model.")


_QWEN_TARGET_COUNTS = {
    "self_attn.o_proj": 16,
    "linear_attn.out_proj": 48,
    "mlp.down_proj": 64,
}
_QWEN_TARGET_SHAPES = {
    "self_attn.o_proj": (5120, 6144),
    "linear_attn.out_proj": (5120, 6144),
    "mlp.down_proj": (5120, 17408),
}


@dataclass
class AbliterationParameters:
    max_weight: float
    max_weight_position: float
    min_weight: float
    min_weight_distance: float


class Model:
    model: PreTrainedModel | PeftModel
    tokenizer: PreTrainedTokenizerBase
    # Set for multimodal models, None for text-only ones.
    processor: ProcessorMixin | None
    peft_config: LoraConfig
    dtype: torch.dtype
    ara_targets: tuple[ara.TargetModule, ...]
    adapter_initial_state: ara.AdapterInitialState | None

    def __init__(self, settings: Settings):
        self.settings = settings
        self.needs_reload = False

        self.revision_kwargs = {}
        if settings.model_commit is not None:
            self.revision_kwargs["revision"] = settings.model_commit

        print(f"* Chat template kwargs: [bold]{settings.chat_template_kwargs}[/]")
        print(f"* Generation kwargs: [bold]{settings.generation_kwargs}[/]")

        print()
        print(f"Loading model [bold]{settings.model}[/]...")

        self.tokenizer = cast(
            PreTrainedTokenizerBase,
            AutoTokenizer.from_pretrained(settings.model, **self.revision_kwargs),
        )

        self.processor = None
        if (
            get_model_class(settings.model, revision=settings.model_commit)
            == AutoModelForImageTextToText
        ):
            self.processor = AutoProcessor.from_pretrained(
                settings.model,
                **self.revision_kwargs,
            )

        if self.tokenizer.pad_token is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token

        # CRITICAL: Always use left-padding for decoder-only models during generation.
        #           Right-padding causes empty outputs because the model sees PAD tokens
        #           after the prompt and thinks the sequence is complete.
        self.tokenizer.padding_side = "left"

        self.model = None  # ty:ignore[invalid-assignment]
        self.max_memory = (
            {int(k) if k.isdigit() else k: v for k, v in settings.max_memory.items()}
            if settings.max_memory
            else None
        )

        self.trusted_models = set()

        for dtype in settings.dtypes:
            print(f"* Trying dtype [bold]{dtype}[/]...")

            try:
                quantization_config = self._get_quantization_config(dtype)

                extra_kwargs = {}
                if quantization_config is not None:
                    extra_kwargs["quantization_config"] = quantization_config

                self.model = get_model_class(
                    settings.model, revision=settings.model_commit
                ).from_pretrained(
                    settings.model,
                    dtype=dtype,
                    device_map=settings.device_map,
                    max_memory=self.max_memory,
                    trust_remote_code=True
                    if settings.model in self.trusted_models
                    else None,
                    **self.revision_kwargs,
                    **extra_kwargs,
                )

                self.dtype = self.model.dtype

                # If we reach this point and the model requires trust_remote_code,
                # the user must have agreed when prompted to execute remote code,
                # because from_pretrained raises an exception otherwise.
                self.trusted_models.add(settings.model)

                # A test run can reveal dtype-related problems such as the infamous
                # "RuntimeError: probability tensor contains either `inf`, `nan` or element < 0"
                # (https://github.com/meta-llama/llama/issues/380).
                self.generate(
                    [
                        Prompt(
                            system=settings.system_prompt,
                            user="What is 1+1?",
                        )
                    ],
                    max_new_tokens=1,
                )
            except Exception as error:
                self.model = None  # ty:ignore[invalid-assignment]
                empty_cache()

                formatted = format_exception(error)
                if "\n" in formatted:
                    print(f"* [red]Failed:\n{formatted}[/]")
                else:
                    print(f"* [red]Failed ({formatted})[/]")

                continue

            if settings.quantization == QuantizationMethod.BNB_4BIT:
                print("* Quantized to 4-bit precision")

            break

        if self.model is None:
            raise Exception("Failed to load model with all configured dtypes.")

        self.ara_targets = ()
        self.adapter_initial_state = None
        self._apply_lora()

        print(f"* Transformer model with [bold]{len(self.get_layers())}[/] layers")

        all_components = {}
        for layer_index in range(len(self.get_layers())):
            for component, modules in self.get_layer_modules(layer_index).items():
                if component not in all_components:
                    all_components[component] = 0
                all_components[component] += len(modules)

        print("* Abliterable components:")
        for component, count in all_components.items():
            print(f"  * [bold]{component}[/]: [bold]{count}[/] modules total")
        self._validate_ara_runtime()

    def _validate_ara_runtime(self) -> None:
        if not (
            self.settings.abliteration_method == AbliterationMethod.ARA
            and self.settings.model == "Qwen/Qwen3.8-27B"
        ):
            return
        counts = dict.fromkeys(_QWEN_TARGET_COUNTS, 0)
        unmatched = []
        for target in self.ara_targets:
            name = target.full_name
            key = next((item for item in counts if item in name), "")
            if key:
                counts[key] += 1
            else:
                unmatched.append(name)
        invalid_shapes = [
            (target.full_name, (target.out_features, target.in_features))
            for target in self.ara_targets
            if any(
                name in target.full_name
                and (target.out_features, target.in_features) != shape
                for name, shape in _QWEN_TARGET_SHAPES.items()
            )
        ]
        if (
            counts != _QWEN_TARGET_COUNTS
            or len(self.ara_targets) != 128
            or unmatched
            or invalid_shapes
        ):
            raise RuntimeError(
                f"Qwen CARA module inventory mismatch: counts={counts}, "
                f"total={len(self.ara_targets)}, unmatched={unmatched}, "
                f"invalid_shapes={invalid_shapes}"
            )
        devices = {
            str(next(target.module.parameters()).device) for target in self.ara_targets
        }
        if not {"cuda:0", "cuda:1"}.issubset(devices):
            raise RuntimeError(
                f"Qwen CARA targets are not distributed across two GPUs: {devices}"
            )
        for index in (0, 1):
            free_bytes, _ = torch.cuda.mem_get_info(index)
            if free_bytes < 1.5 * 1024**3:
                raise RuntimeError(f"cuda:{index} has less than 1.5 GiB free")

    def _apply_lora(self):
        assert isinstance(self.model, PreTrainedModel)

        target_modules_set: set[str] = set()

        module_id_to_full_name = {
            id(module): module_name
            for module_name, module in self.model.named_modules()
        }

        for layer_index in range(len(self.get_layers())):
            for modules in self.get_layer_modules(layer_index).values():
                for module in modules:
                    full_name = module_id_to_full_name.get(id(module))
                    if full_name is not None:
                        target_modules_set.add(full_name)

        target_modules = sorted(target_modules_set)

        if self.settings.abliteration_method == AbliterationMethod.ARA:
            lora_rank = self.settings.ara_lora_rank
        elif self.settings.row_normalization != RowNormalization.FULL:
            # Rank 1 is sufficient for directional ablation without renormalization.
            lora_rank = 1
        else:
            # Row magnitude preservation introduces nonlinear effects.
            lora_rank = self.settings.full_normalization_lora_rank

        self.peft_config = LoraConfig(
            r=lora_rank,
            target_modules=target_modules,
            lora_alpha=lora_rank,  # Apply adapter at full strength.
            lora_dropout=0,
            bias="none",
            use_rslora=False,
            use_dora=False,
            fan_in_fan_out=False,
            revision=self.settings.model_commit,
            task_type="CAUSAL_LM",
        )

        self.model = cast(PeftModel, get_peft_model(self.model, self.peft_config))

        display_targets = sorted({name.rsplit(".", 1)[-1] for name in target_modules})
        print(
            f"* LoRA adapters initialized (target types: {', '.join(display_targets)})"
        )

        if self.settings.abliteration_method == AbliterationMethod.ARA:
            self.ara_targets = self.get_target_modules()
            self.adapter_initial_state = ara.snapshot_adapter_state(
                self.ara_targets, cast(int, self.settings.seed)
            )
        self._verify_model_fingerprint()

    def _get_quantization_config(self, dtype: str) -> BitsAndBytesConfig | None:
        """
        Creates quantization config based on settings.

        Args:
            dtype: The dtype string (e.g., "auto", "bfloat16")

        Returns:
            BitsAndBytesConfig or None
        """
        if self.settings.quantization == QuantizationMethod.BNB_4BIT:
            # BitsAndBytesConfig expects a torch.dtype, not a string.
            if dtype == "auto":
                compute_dtype = torch.bfloat16
            else:
                compute_dtype = getattr(torch, dtype)

            return BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_compute_dtype=compute_dtype,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_use_double_quant=True,
            )
        return None

    def get_merged_model(self) -> PreTrainedModel:
        # Guard against calling this method at the wrong time.
        assert isinstance(self.model, PeftModel)
        # Check if we need special handling for quantized models
        if self.settings.quantization == QuantizationMethod.BNB_4BIT:
            # Quantized models need special handling - we must reload the base model
            # in full precision to merge the LoRA adapters

            # Get the adapter state dict before we do anything
            adapter_state = {}
            for name, param in self.model.named_parameters():
                if "lora_" in name:
                    adapter_state[name] = param.data.clone().cpu()

            # Load base model in full precision on CPU to avoid VRAM issues
            print("* Loading base model on CPU (this may take a while)...")
            base_model = get_model_class(
                self.settings.model, revision=self.settings.model_commit
            ).from_pretrained(
                self.settings.model,
                torch_dtype=self.model.dtype,
                device_map="cpu",
                trust_remote_code=True
                if self.settings.model in self.trusted_models
                else None,
                **self.revision_kwargs,
            )
            self._verify_reloaded_base(base_model)

            # Apply LoRA adapters to the CPU model
            print("* Applying LoRA adapters...")
            peft_model = get_peft_model(base_model, self.peft_config)

            # Copy the trained adapter weights
            for name, param in peft_model.named_parameters():
                if name in adapter_state:
                    param.data = adapter_state[name].to(param.device)

            # Merge and unload
            print("* Merging LoRA adapters into base model...")
            merged_model = peft_model.merge_and_unload()
            return merged_model
        else:
            # Non-quantized model - can merge directly
            print("* Merging LoRA adapters into base model...")
            merged_model = self.model.merge_and_unload()
            # merge_and_unload() modifies self.model in-place, destroying LoRA adapters.
            # Mark for full reload if user switches trials later.
            self.needs_reload = True
            return merged_model

    def reset_model(self):
        """
        Resets the model to a clean state for the next trial or evaluation.

        Behavior:
        - Fast path: If the same model is loaded and doesn't need full reload,
          resets LoRA adapter weights to zero (identity transformation).
        - Slow path: If switching models or after merge_and_unload(),
          performs full model reload with quantization config.
        """

        # If a prior model load was interrupted/cancelled mid-process, self.model will be None.
        current_model = None
        if self.model is not None:
            current_model = getattr(self.model.config, "name_or_path", None)

        if current_model == self.settings.model and not self.needs_reload:
            if self.settings.abliteration_method == AbliterationMethod.ARA:
                assert self.adapter_initial_state is not None
                ara.restore_adapter_state(self.ara_targets, self.adapter_initial_state)
                return
            # Reset LoRA adapters to zero (identity transformation).
            for name, module in self.model.named_modules():
                if "lora_B" in name and hasattr(module, "weight"):
                    torch.nn.init.zeros_(module.weight)
            return

        # Purge existing model object from memory to make space.
        self.model = None  # ty:ignore[invalid-assignment]
        empty_cache()

        quantization_config = self._get_quantization_config(
            str(self.dtype).split(".")[-1]
        )

        # Build kwargs, only include quantization_config if it's not None.
        extra_kwargs = {}
        if quantization_config is not None:
            extra_kwargs["quantization_config"] = quantization_config

        self.model = get_model_class(
            self.settings.model, revision=self.settings.model_commit
        ).from_pretrained(
            self.settings.model,
            dtype=self.dtype,
            device_map=self.settings.device_map,
            max_memory=self.max_memory,
            trust_remote_code=True
            if self.settings.model in self.trusted_models
            else None,
            **self.revision_kwargs,
            **extra_kwargs,
        )

        self._apply_lora()

        self.needs_reload = False

    def load_adapter_for_evaluation(self, path: str) -> None:
        """Load an exported adapter under a separate name and activate it."""

        assert isinstance(self.model, PeftModel)
        self.model.load_adapter(path, adapter_name="candidate")
        self.model.set_adapter("candidate")
        self._verify_model_fingerprint()
        self._validate_ara_runtime()

    def get_layers(self) -> ModuleList:
        model = self.model

        # Unwrap PeftModel (always true after _apply_lora)
        if isinstance(model, PeftModel):
            model = model.base_model.model

        with suppress(Exception):
            return model.model.language_model.layers

        # Text-only models.
        return model.model.layers

    def get_layer_modules(self, layer_index: int) -> dict[str, list[Module]]:
        layer = self.get_layers()[layer_index]
        modules = discover_layer_modules(layer, self.settings.target_components)
        if not modules:
            children = sorted(name for name, _ in layer.named_children())
            raise RuntimeError(
                f"No target modules found in layer {layer_index} "
                f"({type(layer).__name__}); requested={self.settings.target_components}, "
                f"children={children}"
            )
        return modules

    def get_target_modules(self) -> tuple[ara.TargetModule, ...]:
        """Return stable, de-duplicated records for all configured projections."""
        names = {id(module): name for name, module in self.model.named_modules()}
        seen: dict[int, str] = {}
        targets = []
        for layer_index in range(len(self.get_layers())):
            for component, modules in self.get_layer_modules(layer_index).items():
                for module_index, module in enumerate(modules):
                    previous = seen.get(id(module))
                    if previous is not None and previous != component:
                        raise RuntimeError(
                            f"module mapped to {previous} and {component}"
                        )
                    if previous is not None:
                        continue
                    seen[id(module)] = component
                    base = getattr(module, "base_layer", module)
                    targets.append(
                        ara.TargetModule(
                            key=ara.ModuleKey(layer_index, component, module_index),
                            full_name=names[id(module)],
                            module=module,
                            in_features=int(getattr(base, "in_features")),
                            out_features=int(getattr(base, "out_features")),
                        )
                    )
        return tuple(targets)

    def capture_ara_module_io(self, prompts: list[Prompt]) -> ara.ModuleIO:
        """Capture CARA calibration tensors through the model generation path."""

        def generate_one_token(batch: list[Prompt]) -> Any:
            return self.generate(batch, max_new_tokens=1, use_cache=False)

        config = ara.ARACaptureConfig(batch_size=self.settings.ara_capture_batch_size)
        return ara.capture_module_io(
            self.ara_targets, prompts, generate_one_token, config
        )

    def _model_manifest(self) -> dict[str, Any]:
        config = self.model.config
        targets = [
            {
                "name": _base_target_name(target.full_name),
                "shape": [target.out_features, target.in_features],
            }
            for target in self.get_target_modules()
        ]
        template = str(self.tokenizer.chat_template or "")
        return {
            "model": self.settings.model,
            "requested_revision": self.settings.model_commit,
            "commit": getattr(config, "_commit_hash", None),
            "model_type": getattr(config, "model_type", None),
            "layers": len(self.get_layers()),
            "targets": targets,
            "chat_template_sha256": hashlib.sha256(template.encode()).hexdigest(),
        }

    def _verify_reloaded_base(self, base_model: PreTrainedModel) -> None:
        modules = dict(base_model.named_modules())
        targets = []
        for expected in self.model_manifest["targets"]:
            module = modules.get(expected["name"])
            weight = getattr(module, "weight", None)
            if weight is None or len(weight.shape) != 2:
                raise RuntimeError(f"merged base has no target {expected['name']}")
            targets.append({"name": expected["name"], "shape": list(weight.shape)})
        try:
            layers = base_model.model.language_model.layers  # ty:ignore[unresolved-attribute]
        except AttributeError:
            layers = base_model.model.layers  # ty:ignore[unresolved-attribute]
        config = base_model.config
        manifest = {
            **self.model_manifest,
            "commit": getattr(config, "_commit_hash", None),
            "model_type": getattr(config, "model_type", None),
            "layers": len(layers),
            "targets": targets,
        }
        if _manifest_fingerprint(manifest) != self.model_fingerprint:
            raise RuntimeError(
                "merged base model fingerprint does not match loaded model"
            )

    def _verify_model_fingerprint(self) -> None:
        manifest = self._model_manifest()
        fingerprint = _manifest_fingerprint(manifest)
        previous = getattr(self, "model_fingerprint", None)
        if previous is not None and previous != fingerprint:
            raise RuntimeError("model fingerprint changed during reload")
        self.model_manifest = manifest
        self.model_fingerprint = fingerprint

    def get_abliterable_components(self) -> list[str]:
        components: set[str] = set()

        for layer_index in range(len(self.get_layers())):
            components.update(self.get_layer_modules(layer_index).keys())

        return sorted(components)

    def generate(
        self,
        prompts: list[Prompt],
        **kwargs: Any,
    ) -> tuple[BatchEncoding, GenerateDecoderOnlyOutput | LongTensor]:
        chats = [
            [
                {"role": "system", "content": prompt.system},
                {"role": "user", "content": prompt.user},
            ]
            for prompt in prompts
        ]

        chat_prompts = cast(list[str], self._render_chat_template(chats))

        if self.settings.response_prefix:
            # Append the common response prefix to the prompts so that evaluation happens
            # at the point where responses start to differ for different prompts.
            chat_prompts = [
                prompt + self.settings.response_prefix for prompt in chat_prompts
            ]

        inputs = self.tokenizer(
            chat_prompts,
            return_tensors="pt",
            padding=True,
            return_token_type_ids=False,
        ).to(self.model.device)

        # FIXME: The type checker has been disabled here because of the extremely complex
        #        interplay between different generate() signatures and dynamic delegation.
        generation_kwargs = merge_generation_kwargs(
            self.settings.generation_kwargs,
            kwargs,
            {"pad_token_id": self.tokenizer.pad_token_id},
        )
        outputs = self.model.generate(**inputs, **generation_kwargs)  # ty:ignore[call-non-callable]

        return inputs, outputs

    def get_responses(
        self,
        prompts: list[Prompt],
        skip_special_tokens: bool = False,
    ) -> list[str]:
        inputs, outputs = self.generate(
            prompts,
            max_new_tokens=self.settings.max_response_length,
        )

        return self.tokenizer.batch_decode(
            # Extract the newly generated part.
            # This cast is valid because the input_ids property is a Tensor
            # if the tokenizer is invoked with return_tensors="pt", as above.
            outputs[:, cast(Tensor, inputs["input_ids"]).shape[1] :],
            skip_special_tokens=skip_special_tokens,
        )

    def get_responses_batched(
        self,
        prompts: list[Prompt],
        skip_special_tokens: bool = False,
    ) -> list[str]:
        responses = []
        for batch in batchify(prompts, self.settings.batch_size):
            for response in self.get_responses(
                batch,
                skip_special_tokens=skip_special_tokens,
            ):
                responses.append(response)

        return responses

    def get_residuals(self, prompts: list[Prompt]) -> Tensor:
        # We only generate one token, and we return the residual vectors
        # at that token position, for each prompt and layer.
        _, outputs = self.generate(
            prompts,
            max_new_tokens=1,
            output_hidden_states=True,
            return_dict_in_generate=True,
            # KV cache is unnecessary here because we only need the hidden states
            # for the first generated token.
            use_cache=False,
        )

        # This cast is valid because GenerateDecoderOnlyOutput is the return type
        # of model.generate with return_dict_in_generate=True.
        outputs = cast(GenerateDecoderOnlyOutput, outputs)

        # Hidden states for the first (only) generated token.
        # This cast is valid because we passed output_hidden_states=True above.
        hidden_states = cast(tuple[tuple[FloatTensor]], outputs.hidden_states)[0]

        # The returned tensor has shape (prompt, layer, component).
        residuals = torch.stack(
            # layer_hidden_states has shape (prompt, position, component),
            # so this extracts the hidden states at the end of each prompt,
            # and stacks them up over the layers.
            [layer_hidden_states[:, -1, :] for layer_hidden_states in hidden_states],
            dim=1,
        )

        # Upcast the data type to avoid precision (bfloat16) or range (float16)
        # problems during calculations involving residual vectors.
        residuals = residuals.to(torch.float32)

        if 0 <= self.settings.winsorization_quantile < 1:
            # Apply symmetric winsorization to each layer of the per-prompt residuals.
            abs_residuals = torch.abs(residuals)
            # Get the (prompt, layer, 1) quantiles of the (prompt, layer, component) residuals.
            thresholds = torch.quantile(
                abs_residuals,
                self.settings.winsorization_quantile,
                dim=2,
                keepdim=True,
            )
            residuals = torch.clamp(residuals, -thresholds, thresholds)

        if self.settings.offload_outputs_to_cpu:
            residuals = residuals.cpu()
            empty_cache()

        return residuals

    def get_residuals_batched(self, prompts: list[Prompt]) -> Tensor:
        residuals = []

        for batch in batchify(prompts, self.settings.batch_size):
            residuals.append(self.get_residuals(batch))

        return torch.cat(residuals, dim=0)

    def get_residuals_mean(self, prompts: list[Prompt]) -> Tensor:
        if not prompts:
            raise ValueError("prompts must not be empty")

        running_sum = None
        total_count = 0

        for batch in batchify(prompts, self.settings.batch_size):
            batch_residuals = self.get_residuals(batch)

            # Accumulate in high precision on CPU to reduce peak VRAM usage.
            batch_sum = batch_residuals.sum(dim=0, dtype=torch.float64).cpu()

            if running_sum is None:
                running_sum = batch_sum
            else:
                running_sum += batch_sum

            total_count += batch_residuals.shape[0]

        assert running_sum is not None

        return (running_sum / total_count).to(torch.float32)

    def get_logits(self, prompts: list[Prompt]) -> Tensor:
        # We only generate one token, and we return the raw logits over the vocabulary
        # at that token position, for each prompt.
        _, outputs = self.generate(
            prompts,
            max_new_tokens=1,
            output_logits=True,
            return_dict_in_generate=True,
            use_cache=False,
        )

        # This cast is valid because GenerateDecoderOnlyOutput is the return type
        # of model.generate with return_dict_in_generate=True.
        outputs = cast(GenerateDecoderOnlyOutput, outputs)

        # Logits for the first (only) generated token.
        # Use raw logits, not processed generation scores; processors can insert
        # -inf for suppressed tokens, which can make KL divergence evaluate to NaN.
        # This cast is valid because we passed output_logits=True above.
        logits = cast(tuple[FloatTensor], outputs.logits)[0]

        # The returned tensor has shape (prompt, token).
        if self.settings.offload_outputs_to_cpu:
            del outputs
            logits = logits.cpu()
            empty_cache()

        return logits

    def get_logits_batched(self, prompts: list[Prompt]) -> Tensor:
        logits = []

        for batch in batchify(prompts, self.settings.batch_size):
            logits.append(self.get_logits(batch))

        return torch.cat(logits, dim=0)

    def stream_chat_response(self, chat: list[dict[str, str]]) -> str:
        chat_prompt = cast(str, self._render_chat_template(chat))

        inputs = self.tokenizer(
            chat_prompt,
            return_tensors="pt",
            return_token_type_ids=False,
        ).to(self.model.device)

        streamer = TextStreamer(
            # The TextStreamer constructor annotates this parameter with the AutoTokenizer
            # type, which makes no sense because AutoTokenizer is a factory class,
            # not a base class that tokenizers inherit from.
            self.tokenizer,  # ty:ignore[invalid-argument-type]
            skip_prompt=True,
            skip_special_tokens=True,
        )

        # FIXME: The type checker has been disabled here because of the extremely complex
        #        interplay between different generate() signatures and dynamic delegation.
        generation_kwargs = merge_generation_kwargs(
            self.settings.generation_kwargs,
            {"max_new_tokens": 4096},
            {
                "pad_token_id": self.tokenizer.pad_token_id,
                "streamer": streamer,
            },
        )
        outputs = self.model.generate(**inputs, **generation_kwargs)  # ty:ignore[call-non-callable]

        # This cast is valid because str is the return type
        # when passing a sequence of token IDs.
        return cast(
            str,
            self.tokenizer.decode(
                outputs[0, inputs["input_ids"].shape[1] :],
                skip_special_tokens=True,
            ),
        )

    def _render_chat_template(self, chat: Any) -> str | list[str]:
        rendered = cast(
            str | list[str],
            self.tokenizer.apply_chat_template(
                chat,
                add_generation_prompt=True,
                tokenize=False,
                **self.settings.chat_template_kwargs,
            ),
        )
        prompts = [rendered] if isinstance(rendered, str) else rendered
        if self.settings.chat_template_kwargs.get("enable_thinking") is False and any(
            prompt.count("<think>") != prompt.count("</think>") for prompt in prompts
        ):
            raise RuntimeError("chat template rendered an unclosed <think> block")
        return rendered
