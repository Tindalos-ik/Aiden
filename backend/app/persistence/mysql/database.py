"""MySQL 连接、Engine 缓存和短生命周期 Session 工具。

连接串只从进程环境变量 DATABASE_URL 读取，避免在代码或 Alembic 配置中保存密码。
模块导入不会连接数据库，也不会自动创建或修改表；所有业务函数应尽早关闭 Session，
特别是模型异步流式调用期间不得持有同步数据库会话。
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from functools import lru_cache
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


def database_url_from_env() -> str:
    """从进程环境中读取连接 URL，并在缺失时给出明确配置提示。

    这里不自动加载 .env 文件，也不在异常信息里回显实际 URL，避免意外暴露密码。
    """
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError(
            "DATABASE_URL is required, for example "
            "mysql+pymysql://aiden:password@127.0.0.1:3307/aiden?charset=utf8mb4"
        )
    return database_url


@lru_cache(maxsize=4)
def _engine_for_url(database_url: str) -> Engine:
    """按连接 URL 缓存 Engine；创建 Engine 本身不会立即打开数据库连接。

    pool_pre_ping 会在借出连接前检查连接是否仍有效；pool_recycle 定期更换长连接，
    降低 MySQL 服务端关闭空闲连接后复用到失效连接的概率。
    """
    return create_engine(database_url, pool_pre_ping=True, pool_recycle=1800)


def get_engine() -> Engine:
    """读取当前 DATABASE_URL 对应的缓存 Engine。

    仅调用本函数时才检查 DATABASE_URL；导入模块不会连库，也不会自动创建表。
    """
    return _engine_for_url(database_url_from_env())


@lru_cache(maxsize=4)
def _session_factory_for_url(database_url: str) -> sessionmaker[Session]:
    """为每个 URL 复用 Session 工厂，并将会话对象绑定到对应的 Engine。"""
    return sessionmaker(bind=_engine_for_url(database_url), autoflush=False, expire_on_commit=False)


def get_session_factory() -> sessionmaker[Session]:
    """返回当前 DATABASE_URL 对应的 SQLAlchemy Session 工厂。"""
    return _session_factory_for_url(database_url_from_env())


def get_session() -> Iterator[Session]:
    """提供适用于 FastAPI Depends 的 Session 生成器。

    这里只负责创建和关闭会话，不擅自提交事务；路由或服务层可根据业务成功/失败
    显式 commit/rollback，生成器退出时无论如何都会关闭连接资源。
    """
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """以一个事务作用域执行数据库操作。

    正常退出 session.begin() 时提交；代码抛出异常时由 SQLAlchemy 回滚，
    外层 Session 上下文随后关闭会话。适合脚本或服务中的短事务。
    """
    with get_session_factory()() as session:
        with session.begin():
            yield session
