# 第三方组件许可说明

rss-watch 自有部分采用 [MIT 许可证](LICENSE)。第三方组件分别适用其自身许可证，项目的 MIT 声明不替代这些条款。

以下清单对应当前 `uv.lock`。Wheel 和 sdist 通过依赖声明引用这些 Python 包，由安装器另行安装；Docker 镜像包含运行依赖。再分发包含依赖的环境或镜像时，应保留相应组件的版权、许可正文、免责声明及适用的 NOTICE。

## 直接运行依赖

| 组件 | 锁定版本 | 许可证 |
| --- | --- | --- |
| typer | 0.27.2 | MIT；内置 `_click` 另适用 BSD-3-Clause |
| rich | 14.3.4 | MIT |
| httpx | 0.28.1 | BSD-3-Clause |
| feedparser | 6.0.14 | BSD-2-Clause |
| fastapi | 0.143.0 | MIT |
| uvicorn | 0.54.0 | BSD-3-Clause |
| Jinja2 | 3.1.6 | BSD-3-Clause |
| python-multipart | 0.0.32 | Apache-2.0 |
| tzdata | 2026.5 | Python 分发包声明 Apache-2.0 |

## 间接运行依赖

| 许可证 | 组件及锁定版本 |
| --- | --- |
| MIT | annotated-doc 0.0.5、annotated-types 0.8.0、anyio 4.15.1、h11 0.16.0、markdown-it-py 4.2.0、mdurl 0.1.2、pydantic 2.14.1、pydantic-core 2.50.1、typing-inspection 0.4.4 |
| BSD-3-Clause | click 8.5.0、httpcore 1.0.9、idna 3.19、MarkupSafe 3.0.4、starlette 1.7.0 |
| BSD-2-Clause | Pygments 2.21.0 |
| Apache-2.0 | opentelemetry-api 1.45.1 |
| PSF-2.0 | feedparser-sgmllib 2.1.0、typing-extensions 4.16.0；保留随包完整历史许可文本 |
| ISC | shellingham 1.5.4 |
| MPL-2.0 | certifi 2026.7.22 |

Windows 条件依赖另含 colorama 0.4.6，采用 BSD-3-Clause；Linux 镜像不安装该条件依赖。升级锁文件时应同步核对本清单。

## 随包声明和源码获取

各 Python 包的许可文件通常位于其安装目录的 `*.dist-info/licenses/`，部分包放在 `*.dist-info/` 根目录。以下材料也应保留：

- `typer/_click/LICENSE.txt` 中内置 Click 代码的 BSD 声明。
- markdown-it-py 的 `LICENSE` 和 `LICENSE.markdown-it` 两份 MIT 声明。
- Pygments 的 `LICENSE` 和 `AUTHORS`，以及 PSF 系组件随包附带的完整许可正文。
- Apache-2.0 组件的许可、版权及上游适用的 NOTICE；修改时依条款标明改动。

certifi 的证书包适用 [MPL-2.0](https://www.mozilla.org/en-US/MPL/2.0/)。当前锁定版本的源码可从 [certifi 2026.7.22 的源码分发包](https://files.pythonhosted.org/packages/a3/c2/24167ea9858356b47a87a50d39908bfdb72ceeefe0041586e704e5376b3a/certifi-2026.7.22.tar.gz) 获取，其下载地址和 SHA-256 也记录在 `uv.lock` 中。再分发受 MPL 覆盖的可执行形式时，应告知接收者相应源码的获取方式；若修改了受覆盖文件，应提供对应的修改后源码。该要求适用于 MPL 覆盖的部分。

Docker 基础镜像中的 Debian、CPython 和系统库，以及原生扩展所含的组件，继续适用各自的条款；应保留其版权及许可文件，并按适用条款提供源码获取方式。本清单是 Python 依赖索引，不能替代各组件的完整法律材料。
