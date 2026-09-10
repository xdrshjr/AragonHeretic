# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026  Philipp Emanuel Weidmann <pew@worldwidemann.com> + contributors

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
from .ara_runtime import (
    capture_model_trajectory,
    continuation_cache_identity,
    create_model_manifest,
    manifest_fingerprint,
    render_prompt_texts,
    score_model_continuations,
    validate_model_prefix_groups,
    validate_runtime_guard,
    verify_model_fingerprint,
    verify_reloaded_base,
)
from .config import AbliterationMethod, QuantizationMethod, RowNormalization, Settings
from .system import empty_cache
from .targeting import discover_layer_modules
from .utils import Prompt, batchify, format_exception, print

# Compatibility alias for point-v1 fixtures and downstream imports.
_manifest_fingerprint = manifest_fingerprint


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


@dataclass
class AbliterationParameters:
    max_weight: float
    max_weight_position: float
    min_weight: float
    min_weight_distance: float


class Model:
    model: PreTrainedModel | PeftModel
    model_fingerprint: str
    model_manifest: dict[str, Any]
    tokenizer: PreTrainedTokenizerBase
    # Set for multimodal models, None for text-only ones.
    processor: ProcessorMixin | None
    peft_config: LoraConfig
    dtype: torch.dtype
    ara_targets: tuple[ara.TargetModule, ...]
    adapter_initial_state: ara.AdapterInitialState | None
    ara_runtime_report: Any

    def __init__(self, settings: Settings):
        """Load the base model, attach LoRA, and validate target inventory."""
        self.settings = settings
        self.needs_reload = False
        self.revision_kwargs = {}
        if settings.model_commit is not None:
            self.revision_kwargs["revision"] = settings.model_commit
        print(f"* Chat template kwargs: [bold]{settings.chat_template_kwargs}[/]")
        print(f"* Generation kwargs: [bold]{settings.generation_kwargs}[/]")
        print()
        print(f"Loading model [bold]{settings.model}[/]...")
        self._load_tokenizer()
        self.model = None  # ty:ignore[invalid-assignment]
        self.max_memory = (
            {
                int(key) if key.isdigit() else key: value
                for key, value in settings.max_memory.items()
            }
            if settings.max_memory
            else None
        )
        self.trusted_models = set()
        self._load_supported_dtype()
        self.ara_targets = ()
        self.adapter_initial_state = None
        self._apply_lora()
        self._report_model_structure()
        self._validate_ara_runtime()

    def _load_tokenizer(self) -> None:
        settings = self.settings
        self.tokenizer = cast(
            PreTrainedTokenizerBase,
            AutoTokenizer.from_pretrained(settings.model, **self.revision_kwargs),
        )
        self.processor = None
        model_class = get_model_class(settings.model, revision=settings.model_commit)
        if model_class == AutoModelForImageTextToText:
            self.processor = AutoProcessor.from_pretrained(
                settings.model, **self.revision_kwargs
            )
        if self.tokenizer.pad_token is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token
        self.tokenizer.padding_side = "left"

    def _try_dtype(self, dtype: str) -> bool:
        settings = self.settings
        quantization = self._get_quantization_config(dtype)
        extra = {"quantization_config": quantization} if quantization else {}
        try:
            model_class = get_model_class(
                settings.model, revision=settings.model_commit
            )
            self.model = model_class.from_pretrained(
                settings.model,
                dtype=dtype,
                device_map=settings.device_map,
                max_memory=self.max_memory,
                trust_remote_code=True
                if settings.model in self.trusted_models
                else None,
                **self.revision_kwargs,
                **extra,
            )
            self.dtype = self.model.dtype
            self.trusted_models.add(settings.model)
            self.generate(
                [Prompt(system=settings.system_prompt, user="What is 1+1?")],
                max_new_tokens=1,
            )
            return True
        except Exception as error:
            self.model = None  # ty:ignore[invalid-assignment]
            empty_cache()
            formatted = format_exception(error)
            separator = ":\n" if "\n" in formatted else " ("
            suffix = "" if "\n" in formatted else ")"
            print(f"* [red]Failed{separator}{formatted}{suffix}[/]")
            return False

    def _load_supported_dtype(self) -> None:
        for dtype in self.settings.dtypes:
            print(f"* Trying dtype [bold]{dtype}[/]...")
            if not self._try_dtype(dtype):
                continue
            if self.settings.quantization == QuantizationMethod.BNB_4BIT:
                print("* Quantized to 4-bit precision")
            return
        raise RuntimeError("Failed to load model with all configured dtypes.")

    def _report_model_structure(self) -> None:
        layers = self.get_layers()
        print(f"* Transformer model with [bold]{len(layers)}[/] layers")
        components: dict[str, int] = {}
        for layer_index in range(len(layers)):
            for component, modules in self.get_layer_modules(layer_index).items():
                components[component] = components.get(component, 0) + len(modules)
        print("* Abliterable components:")
        for component, count in components.items():
            print(f"  * [bold]{component}[/]: [bold]{count}[/] modules total")

    def _validate_ara_runtime(self) -> None:
        guard = self.settings.ara_runtime_guard
        if self.settings.abliteration_method != AbliterationMethod.ARA or guard is None:
            return
        report = validate_runtime_guard(self.ara_targets, guard)
        self.ara_runtime_report = report

    def _lora_target_names(self) -> list[str]:
        names: set[str] = set()
        by_identity = {
            id(module): module_name
            for module_name, module in self.model.named_modules()
        }
        for layer_index in range(len(self.get_layers())):
            for modules in self.get_layer_modules(layer_index).values():
                for module in modules:
                    full_name = by_identity.get(id(module))
                    if full_name is not None:
                        names.add(full_name)
        return sorted(names)

    def _lora_rank(self) -> int:
        if self.settings.abliteration_method == AbliterationMethod.ARA:
            return self.settings.ara_lora_rank
        if self.settings.row_normalization != RowNormalization.FULL:
            return 1
        return self.settings.full_normalization_lora_rank

    def _apply_lora(self):
        assert isinstance(self.model, PreTrainedModel)
        target_modules = self._lora_target_names()
        lora_rank = self._lora_rank()
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

    def _reset_adapter(self) -> bool:
        current_model = None
        if self.model is not None:
            current_model = getattr(self.model.config, "name_or_path", None)
        if current_model != self.settings.model or self.needs_reload:
            return False
        if self.settings.abliteration_method == AbliterationMethod.ARA:
            assert self.adapter_initial_state is not None
            ara.restore_adapter_state(self.ara_targets, self.adapter_initial_state)
            return True
        for name, module in self.model.named_modules():
            if "lora_B" in name and hasattr(module, "weight"):
                torch.nn.init.zeros_(module.weight)
        return True

    def _reload_model(self) -> None:
        self.model = None  # ty:ignore[invalid-assignment]
        empty_cache()
        quantization_config = self._get_quantization_config(
            str(self.dtype).split(".")[-1]
        )
        extra = (
            {"quantization_config": quantization_config} if quantization_config else {}
        )
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
            **extra,
        )
        self._apply_lora()
        self.needs_reload = False

    def reset_model(self) -> None:
        """Restore an identity adapter or reload a model destroyed by merging."""
        if not self._reset_adapter():
            self._reload_model()

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

    def capture_ara_trajectory_io(self, prompts: list[Prompt]) -> Any:
        """Capture trajectory-v2 module I/O through the shared model facade."""
        return capture_model_trajectory(self, prompts)

    def capture_ara_reference_pair(self, request: Any) -> Any:
        """按冻结 token 捕获 v3 当前/基座参考，并恢复 adapter。"""
        from .ara_refinement_capture import capture_reference_pair
        return capture_reference_pair(self, request)

    def score_ara_sequence_kl(self, bundle: Any) -> Any:
        """计算同一角色固定基座序列的逐题 KL。"""
        from .sequence_scores import score_sequence_kl
        return score_sequence_kl(self, bundle)

    def _model_manifest(self) -> dict[str, Any]:
        return create_model_manifest(self)

    def _verify_reloaded_base(self, base_model: PreTrainedModel) -> None:
        verify_reloaded_base(self, base_model)

    def _verify_model_fingerprint(self) -> None:
        verify_model_fingerprint(self)

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

    def _winsorize_residuals(self, residuals: Tensor) -> Tensor:
        quantile = self.settings.winsorization_quantile
        if not 0 <= quantile < 1:
            return residuals
        thresholds = torch.quantile(torch.abs(residuals), quantile, dim=2, keepdim=True)
        return torch.clamp(residuals, -thresholds, thresholds)

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

        residuals = self._winsorize_residuals(residuals)
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

    def render_prompt_texts(self, prompts: list[Prompt]) -> list[str]:
        """Render prompts with the model's configured chat template."""
        return render_prompt_texts(self, prompts)

    def continuation_cache_identity(
        self,
        continuations: tuple[str, ...],
    ) -> tuple[tuple[tuple[int, ...], ...], str]:
        """Return tokenized prefixes and a stable tokenizer identity."""
        return continuation_cache_identity(self, continuations)

    def validate_prefix_groups(
        self,
        refusal_prefixes: list[str],
        answer_prefixes: list[str],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Validate scorer prefixes against the loaded tokenizer."""
        return validate_model_prefix_groups(
            self,
            refusal_prefixes,
            answer_prefixes,
        )

    def get_continuation_logprobs(
        self,
        prompts: list[Prompt],
        continuations: tuple[str, ...],
        batch_tokens: int,
    ) -> Tensor:
        """Score response prefixes through the shared rendering/model facade."""
        return score_model_continuations(
            self,
            prompts,
            continuations,
            batch_tokens,
        )

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
