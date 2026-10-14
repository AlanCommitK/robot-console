"""应用配置：全部来自环境变量，由 pydantic-settings 读取并做类型校验。

为什么用环境变量而不是配置文件：同一个镜像在本机、CI、docker compose 里只靠环境变量区分，
镜像本身不需要改（"12-factor app" 的做法）。字段名小写、环境变量名大写，
pydantic-settings 默认大小写不敏感，所以 DATABASE_URL 会填进 database_url。
"""

from __future__ import annotations

import socket
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    database_url: str = "postgresql+asyncpg://robot:robot@postgres:5432/robot"
    redis_url: str = "redis://redis:6379/0"
    rabbitmq_url: str = "amqp://guest:guest@rabbitmq:5672/"

    jwt_secret: str = "dev-secret-change-me-at-least-32-bytes-long"
    jwt_expire_minutes: int = Field(default=480, gt=0)

    # 每个 API 副本的名字：写进日志、响应头 X-Replica、命令事件表，用来观察负载均衡。
    replica_id: str = Field(default_factory=socket.gethostname)

    command_ttl_ms: int = Field(default=30000, gt=0)
    ack_timeout_s: float = Field(default=10.0, gt=0)
    # 机器人 ack 之后最多等这么久的结果，超时由 sweeper 判为 failed（result_timeout）。
    # 必须比"最长的合法命令执行时间"更长：慢速走完一段很长的 move_to 也算合法。
    result_timeout_s: float = Field(default=300.0, gt=0)
    robot_online_ttl_s: int = Field(default=5, gt=0)
    telemetry_flush_interval_s: float = Field(default=1.0, gt=0)

    log_level: str = "INFO"
    # 逗号分隔的字符串。不声明成 list[str]：那样 pydantic-settings 会要求环境变量写成 JSON 数组。
    cors_origins: str = "http://localhost:5173"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    """进程内只解析一次环境变量；测试里可以 get_settings.cache_clear() 后重新读取。"""
    return Settings()
