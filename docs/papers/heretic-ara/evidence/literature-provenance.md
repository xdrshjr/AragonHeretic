# 文献来源与元数据核验

```json
{
  "references": [
    {
      "source_id": "lit:arditi2024",
      "bibkey": "arditi2024",
      "url": "https://proceedings.neurips.cc/paper_files/paper/2024/hash/f545448535dfde4f9786555403ab7c49-Abstract-Conference.html",
      "title": "Refusal in Language Models Is Mediated by a Single Direction",
      "authors": [
        "Andy Arditi",
        "Oscar Obeso",
        "Aaquib Syed",
        "Daniel Paleka",
        "Nina Panickssery",
        "Wes Gurnee",
        "Neel Nanda"
      ],
      "year": 2024,
      "version": "NeurIPS 2024, volume 37; arXiv v3",
      "doi": "10.52202/079017-4322",
      "additional_url": "https://arxiv.org/abs/2406.11717v3",
      "accessed_at": "2026-09-08",
      "verified": true
    },
    {
      "source_id": "lit:hu2022lora",
      "bibkey": "hu2022lora",
      "url": "https://arxiv.org/abs/2106.09685",
      "title": "LoRA: Low-Rank Adaptation of Large Language Models",
      "authors": [
        "Edward J. Hu",
        "Yelong Shen",
        "Phillip Wallis",
        "Zeyuan Allen-Zhu",
        "Yuanzhi Li",
        "Shean Wang",
        "Lu Wang",
        "Weizhu Chen"
      ],
      "year": 2022,
      "version": "ICLR 2022; arXiv v2 (2021-10-16)",
      "doi": null,
      "preprint_doi": "10.48550/arXiv.2106.09685",
      "additional_url": "https://iclr.cc/virtual/2022/day/4/29",
      "accessed_at": "2026-09-08",
      "verified": true
    },
    {
      "source_id": "lit:upstreamara",
      "bibkey": "upstreamara",
      "url": "https://github.com/p-e-w/heretic/pull/211",
      "title": "feat: Arbitrary-Rank Ablation (ARA)",
      "authors": [
        "Philipp Emanuel Weidmann",
        "contributors"
      ],
      "year": 2026,
      "version": "edc3b123456c7f86f24d409b838ab3a7226e285e",
      "doi": null,
      "additional_url": "https://raw.githubusercontent.com/p-e-w/heretic/edc3b123456c7f86f24d409b838ab3a7226e285e/src/heretic/model.py",
      "accessed_at": "2026-09-08",
      "verified": true
    },
    {
      "source_id": "lit:heretic",
      "bibkey": "heretic",
      "url": "https://github.com/p-e-w/heretic",
      "title": "Heretic",
      "authors": [
        "Philipp Emanuel Weidmann",
        "contributors"
      ],
      "year": 2026,
      "version": "访问日软件仓库；本地方法描述另绑定worktree SHA-256",
      "doi": null,
      "accessed_at": "2026-09-08",
      "verified": true
    },
    {
      "source_id": "lit:araarchive",
      "bibkey": "araarchive",
      "url": "docs/logs/ara-v1/SUMMARY.md",
      "source_kind": "local_artifact",
      "title": "Qwen3.8-27B ARA v1 全量运行归档",
      "authors": [],
      "year": 2026,
      "version": "2026-09-04原始归档",
      "doi": null,
      "accessed_at": "2026-09-08",
      "verified": true
    }
  ]
}
```

所有条目在 2026-09-08 重新核对。参考文献只承担对应的背景或软件归属论断，外部模型成绩没有并入本轮实验。

- arditi2024：NeurIPS 官方页面核对七位作者、题名、卷 37、2024 年及 DOI；arXiv 页面核对 v3 日期 2024-10-30。正文仅引用作者所研究模型中的方向干预发现，不扩展为所有模型的普遍定理。
- hu2022lora：arXiv 原始页核对八位作者、题名与 2021 年预印本 v2；ICLR 官方 2022 会议索引检索结果核对收录年份。OpenReview 本次直接访问触发浏览器验证，ICLR 日程直接打开也返回错误，未伪称已访问其全文；作者与方法由可访问的 arXiv 原文入口支撑。正文只使用冻结基座与低秩增量背景。BibTeX 以会议年份 2022 编目，明确预印本年份 2021；未把 arXiv DOI 冒充会议 DOI。
- upstreamara：PR 211 原始页面确认 p-e-w 提交、题名及 2026 年记录；固定 revision 的 model.py 版权署名为 Philipp Emanuel Weidmann 与贡献者。固定源码 ara_abliterate 及 ara_lora_abliterate 两条路径均可见，分别核对全矩阵和因子优化；保持、拉近、负距离推远目标来自同一固定文件。PR 页面随时间变更，本文不以其当前状态推断运行代码。
- heretic：官方代码仓库与源码版权核对项目名和作者署名。2026 是本文访问的软件版本年份，不宣称为项目首次发布年份。本文具体方向消融及归一化公式由 source-provenance.md 的本地文件哈希限定。
- araarchive：这是本项目原始制品，不是虚构的发表论文，没有真实作者信息，BibTeX 不设作者。SUMMARY 只提供入口和运行历史说明；120 个 trial 数字从 journal 重放，失败状态取 acceptance.json，不把 SUMMARY 的归因、舍入中位数和外层成功消息当作定量证据。

原始页面以可核验链接保留，不复制网页长段落。所有自定义方法、数值修复及协议论断在 claim-traceability.csv 中另行绑定源码或归档。
