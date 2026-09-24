from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from app.persistence.mysql.models import Base

# 读取 ini 中的日志配置；连接 URL 则由下面的函数从进程环境中取得。
config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Alembic 比较或生成迁移时使用所有已导入模型的统一元数据。
target_metadata = Base.metadata


def _database_url() -> str:
    """迁移只接受进程环境中的 URL，避免把密码写入 alembic.ini 或迁移文件。"""
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("Set DATABASE_URL before running Alembic.")
    return url


def run_migrations_offline() -> None:
    """生成离线 SQL 时只配置方言和元数据，不建立数据库连接。"""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线迁移使用短生命周期连接；退出时显式释放 Engine 连接池。"""
    connectable = create_engine(_database_url(), poolclass=pool.NullPool, pool_pre_ping=True)
    try:
        with connectable.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


# Alembic 的 --sql 模式走离线分支；常规 upgrade/downgrade 才会连接 MySQL 执行 DDL。
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
