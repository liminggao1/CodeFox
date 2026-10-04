# Issue tracker: Local Markdown

本仓库的议题与规格以 Markdown 文件存放在 `.scratch/`。

## Conventions

- 每个功能一个目录：`.scratch/<feature-slug>/`。
- 规格文件：`.scratch/<feature-slug>/spec.md`。
- 实现议题一个票据一个文件：`.scratch/<feature-slug>/issues/<NN>-<slug>.md`，从 `01` 起编号，不用单个合并文件。
- 分诊状态写在议题文件顶部的 `Status:` 行，取值见 `triage-labels.md`。
- 讨论记录追加到文件末尾的 `## Comments`。

## When a skill says "publish to the issue tracker"

在对应的 `.scratch/<feature-slug>/` 下创建新的 Markdown 文件，目录不存在时先创建。

## When a skill says "fetch the relevant ticket"

读取传入路径对应的文件。用户通常会直接给出路径或编号。

## Wayfinding operations

供 `/wayfinder` 使用。**map** 是一个文件，每个票据是它的一个 **child** 文件。

- **Map**：`.scratch/<effort>/map.md`，含 Notes / Decisions-so-far / Fog 正文。
- **Child ticket**：`.scratch/<effort>/issues/NN-<slug>.md`，从 `01` 编号，正文写问题。`Type:` 行记录票据类型（`research`/`prototype`/`grilling`/`task`）；`Status:` 行记录 `claimed`/`resolved`。
- **Blocking**：顶部的 `Blocked by: NN, NN` 行。它列出的每个文件都是 `resolved` 时，该票据解除阻塞。
- **Frontier**：扫描 `.scratch/<effort>/issues/`，取开放、未被阻塞且未认领的文件，编号小的优先。
- **Claim**：开始任何工作前先写 `Status: claimed` 并保存。
- **Resolve**：在 `## Answer` 标题下追加答案，写入 `Status: resolved`，再把上下文指针（要点加链接）追加到 `map.md` 的 Decisions-so-far。
