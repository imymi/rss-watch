# RSS/Atom 解析兼容性实验

本实验用合成样本比较 Go 候选实现与 Python v0.2.0。Python 基准直接调用仓库的 `rss_watch.domain.parse_feed`，本分支起点 `51a128a` 的解析器和依赖锁文件与 `v0.2.0` 一致。两边使用相同的响应字节和最终响应 URL。测试逐条检查是否接受输入、文章条数及顺序、标题、摘要、原始发布时间、链接和 SHA-256 指纹。样本涵盖 RSS 2.0、RSS 0.91、RSS 1.0/RDF、Atom 1.0/0.3、有效空源和异常输入。

## 复现

在本分支根目录准备 Python 3.12 和锁定依赖，再进入实验目录运行 Go 测试。Go 模块要求 Go 1.25 或更新版本。首次需下载 Go 模块和 Python 依赖，测试读取本地样本。

```sh
uv sync --locked --python 3.12
export FEED_PARITY_PYTHON="$PWD/.venv/bin/python"
cd compat/feedparity
go test -count=1 -v ./...
```

测试遇到差异会输出具体样本和字段并返回非零。只看单个样本时可用 `go test -count=1 -v -run 'TestPythonParity/rss-guid-relative.xml'`。

## 2026-11-19 结果

实测工具链为 Go 1.27.4、gofeed 1.5.0、Python 3.12.13 和 feedparser 6.0.14。Python 原有 `tests/test_domain.py` 的 20 项全部通过。

| Go 候选改动 | 完全一致的样本 | 输入接受差异 | 文章条数差异 | 指纹差异 | 文章字段有差异的样本 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 使用 gofeed 通用模型 | 14/29 | 3 | 0 | 13/53 | 11 |
| 以最终响应 URL 补全相对链接 | 17/29 | 3 | 0 | 5/53 | 8 |

指纹分母为双方均接受且文章条数相同的样本中的文章总数。各类差异可以出现在同一个样本中。

| 样本 | 补全相对链接后的差异 |
| --- | --- |
| `rss-guid-relative.xml` | Python 将默认或显式为 permalink 的相对 GUID 解析成绝对 ID。Go 通用模型保留了相对 GUID，且没有补出链接。两篇指纹不同。 |
| `rss-rdf.xml` | Python 使用 `rdf:about` 作为 ID，Go 通用模型用文章链接生成指纹。 |
| `atom-link-selection.xml` | 同时存在 XHTML 和 HTML alternate 时，样本中 Python 选 HTML，Go 选第一个 alternate。无 ID 时指纹随之改变。 |
| `atom-fallback.xml` | 存在空的 `published` 时，Python 保留空值，Go 回退到 `updated`。无 ID 和链接时改变指纹。 |
| `invalid-trailing.xml`、`invalid-unescaped-amp.xml`、`unsupported-json-feed.json` | Go 接受了 Python 拒绝的输入。对已初始化源，这类差异可能使任务入队或提交新的条件请求验证信息。 |
| 其余字段差异 | 只有正文时的摘要回退，以及部分 RSS GUID / Atom ID 补链行为影响消息展示。 |

暂停 Worker 移植。继续前应验证原始 RSS/Atom 字段能否保留 permalink 标志、`rdf:about`、链接顺序和类型、日期字段是否出现，并补上完整文档校验。候选解析器须通过这套差异测试，再用脱敏的代表性真实订阅样本验证一次指纹。现有部署和业务数据未参与实验。
