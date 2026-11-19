# RSS/Atom 解析兼容性实验

本实验用合成样本及脱敏的公开订阅比较 Go 候选实现与 Python v0.2.0。Python 基准直接调用仓库的 `rss_watch.domain.parse_feed`，本分支起点 `51a128a` 的解析器和依赖锁文件与 `v0.2.0` 一致。两边使用相同的响应字节和最终响应 URL。测试逐条检查是否接受输入、文章条数及顺序、标题、摘要、原始发布时间、链接和 SHA-256 指纹。

## 复现

在本分支根目录准备 Python 3.12 和锁定依赖。仓库的 Go 模块要求 Go 1.27 或更新版本。首次需下载 Go 模块和 Python 依赖，测试读取本地样本。

```sh
uv sync --locked --python 3.12
FEED_PARITY_PYTHON="$PWD/.venv/bin/python" go test -count=1 -v ./compat/feedparity
```

测试遇到差异会输出具体样本和字段并返回非零。用 `-run TestPythonParity` 运行合成样本，用 `-run TestRealFeedParity` 复核脱敏订阅。

## 2026-11-19 结果

实测工具链为 Go 1.27.4、Python 3.12.13 和 feedparser 6.0.14。早期实验使用 gofeed 1.5.0。Python 原有 `tests/test_domain.py` 的 20 项全部通过。

| Go 候选改动 | 完全一致的样本 | 输入接受差异 | 文章条数差异 | 指纹差异 | 文章字段有差异的样本 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 使用 gofeed 通用模型 | 14/29 | 3 | 0 | 13/53 | 11 |
| 以最终响应 URL 补全相对链接 | 17/29 | 3 | 0 | 5/53 | 8 |
| 直接读取原始 XML，原有合成样本 | 29/29 | 0 | 0 | 0/53 | 0 |
| 合成样本加四个边界样本 | 33/33 | 0 | 0 | 0/59 | 0 |
| 脱敏的真实订阅 | 9/9 | 0 | 0 | 0/151 | 0 |

指纹分母为双方均接受且文章条数相同的样本中的文章总数。各类差异可以出现在同一个样本中。

| 样本 | 早期 gofeed 版本留下的差异 |
| --- | --- |
| `rss-guid-relative.xml` | Python 将默认或显式为 permalink 的相对 GUID 解析成绝对 ID。Go 通用模型保留了相对 GUID，且没有补出链接。两篇指纹不同。 |
| `rss-rdf.xml` | Python 使用 `rdf:about` 作为 ID，Go 通用模型用文章链接生成指纹。 |
| `atom-link-selection.xml` | 同时存在 XHTML 和 HTML alternate 时，样本中 Python 选 HTML，Go 选第一个 alternate。无 ID 时指纹随之改变。 |
| `atom-fallback.xml` | 存在空的 `published` 时，Python 保留空值，Go 回退到 `updated`。无 ID 和链接时改变指纹。 |
| `invalid-trailing.xml`、`invalid-unescaped-amp.xml`、`unsupported-json-feed.json` | Go 接受了 Python 拒绝的输入。对已初始化源，这类差异可能使任务入队或提交新的条件请求验证信息。 |
| 其余字段差异 | 只有正文时的摘要回退，以及部分 RSS GUID / Atom ID 补链行为影响消息展示。 |

Go 候选改用 `encoding/xml` 直接读取并验证完整文档。按字段出现顺序及是否缺失来复现 GUID / Atom ID、RDF ID、链接、日期和摘要行为。新增样本覆盖 UTF‑16、GB18030、Unicode 相对链接和空 Atom `href`。

## 真实订阅样本

从公开的开发博客、发布动态和新闻订阅取得 9 份文档，分别为 4 份 RSS 2.0、2 份 RSS 1.0/RDF 和 3 份 Atom。保留所有 151 个条目的顺序、字段结构、公开日期原字符串和 HTML 标签结构。域名、路径、查询值、ID、标题、正文、作者等均已替换。两侧使用同一个脱敏的最终响应 URL 解析每份样本。固定样本放在 `testdata/real`，可离线复跑。

结论限于这些样本及覆盖的格式。现有部署和业务数据未参与实验。
