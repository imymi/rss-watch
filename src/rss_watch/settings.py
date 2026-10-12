from __future__ import annotations

import ipaddress
import json
import re
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .db import Database


@dataclass(frozen=True)
class Field:
    label: str
    default: object
    minimum: int = 0
    maximum: int = 0
    group: str = "抓取"
    help: str = ""


FIELDS = {
    "default_interval": Field("新订阅默认间隔（秒）", 300, 60, 86400),
    "max_sources": Field("订阅源数量上限", 100, 1, 1000),
    "fetch_concurrency": Field("抓取并发数", 5, 1, 20),
    "connect_timeout": Field("连接超时（秒）", 10, 1, 120, "网络"),
    "request_deadline": Field("请求总时限（秒）", 30, 1, 300, "网络"),
    "max_body_mib": Field("订阅响应上限（MiB）", 5, 1, 50, "网络"),
    "http_proxy": Field("HTTP 代理", "", group="网络", help="留空沿用部署环境"),
    "https_proxy": Field("HTTPS 代理", "", group="网络", help="http://host:port 或 https://host:port"),
    "no_proxy": Field("代理排除地址", "", group="网络", help="逗号分隔的主机名或 IP；* 表示全部直连"),
    "max_attempts": Field("最多发送次数（含首次）", 5, 1, 10, "推送"),
    "retry_delays": Field("重试间隔（秒）", "30,120,600,1800", group="推送",
                          help="逗号分隔，次数超过间隔数量时沿用最后一个"),
    "send_interval": Field("每机器人发送间隔（秒）", 1, 1, 60, "推送"),
    "fetch_backoff_max": Field("抓取失败退避上限（秒）", 3600, 60, 86400),
    "retention_days": Field("已发送历史保留天数", 90, 1, 3650, "数据",
                            "文章指纹及未完成、失败任务继续保留"),
    "cleanup_hours": Field("历史清理周期（小时）", 24, 1, 168, "数据"),
    "backup_hours": Field("自动备份周期（小时）", 24, 0, 720, "数据", "0 关闭自动备份"),
    "backup_keep": Field("自动备份保留份数", 7, 1, 100, "数据"),
    "timezone": Field("页面时区", "Asia/Shanghai", group="显示"),
    "fetch_paused": Field("暂停抓取", False, group="控制"),
    "delivery_paused": Field("暂停推送", False, group="控制"),
}
NETWORK_KEYS = ("connect_timeout", "request_deadline", "http_proxy", "https_proxy", "no_proxy")


def read_settings(db: Database) -> dict:
    result = {key: field.default for key, field in FIELDS.items()}
    for row in db.rows("SELECT key,value FROM settings"):
        if row["key"] in FIELDS:
            result[row["key"]] = json.loads(row["value"])
    return result


def validate_settings(values: dict) -> dict:
    if set(values) - FIELDS.keys():
        raise ValueError("设置包含未知字段")
    result = {}
    for key, value in values.items():
        field = FIELDS[key]
        if isinstance(field.default, bool):
            if type(value) is not bool:
                raise ValueError(f"{field.label}必须是布尔值")
        elif isinstance(field.default, int):
            if type(value) is not int or not field.minimum <= value <= field.maximum:
                raise ValueError(f"{field.label}应在 {field.minimum} 到 {field.maximum} 之间")
        else:
            if not isinstance(value, str) or len(value) > 2048:
                raise ValueError(f"{field.label}格式无效")
            value = value.strip()
        if key in {"http_proxy", "https_proxy"} and value:
            try:
                url = urlsplit(value)
                port = url.port
                valid = (url.scheme in {"http", "https"} and url.hostname
                         and not url.query and not url.fragment and url.path in {"", "/"}
                         and port != 0 and not any(c.isspace() or ord(c) < 32 for c in value))
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("代理地址格式无效")
        if key == "no_proxy" and value:
            hosts = [host.strip() for host in value.split(",")]
            for host in hosts:
                try:
                    ipaddress.ip_address(host.strip("[]"))
                    valid = True
                except ValueError:
                    valid = host == "*" or bool(re.fullmatch(r"\.?[A-Za-z0-9][A-Za-z0-9.-]*", host))
                if not valid:
                    raise ValueError("代理排除地址应为逗号分隔的主机名或 IP")
        if key == "retry_delays":
            try:
                delays = [int(part.strip()) for part in value.split(",")]
            except ValueError:
                raise ValueError("重试间隔应为逗号分隔的秒数") from None
            if not 1 <= len(delays) <= 10 or any(not 1 <= n <= 86400 for n in delays):
                raise ValueError("重试间隔应包含 1 到 10 个值，每个值在 1 到 86400 秒之间")
            value = ",".join(map(str, delays))
        if key == "timezone":
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError("时区无效，例如 Asia/Shanghai") from None
        result[key] = value
    return result


def save_settings(db: Database, values: dict) -> None:
    values = validate_settings(values)
    with db.transaction() as connection:
        connection.executemany("INSERT OR REPLACE INTO settings VALUES (?,?)",
                               [(k, json.dumps(v, ensure_ascii=False)) for k, v in values.items()])
        connection.execute(
            "INSERT OR REPLACE INTO runtime VALUES ('settings_revision', ?) ",
            (uuid.uuid4().hex,),
        )
