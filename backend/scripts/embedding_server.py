"""本地 BGE-M3 / BGE 系列 embedding 服务（OpenAI 兼容）。

为什么需要这个脚本：BGE-M3 本身只是模型权重，不会响应 HTTP 请求。本项目的离线建库流程按
OpenAI 兼容协议访问 `{EMBEDDING_BASE_URL}/embeddings`（见
`app/services/rag/embedding.py`），所以需要把权重包成一个小服务，让私有化部署也能用同一套
客户端代码，不必改应用逻辑。

启动：

    D:\\bge-m3-env\\Scripts\\python.exe backend\\scripts\\embedding_server.py --model BAAI/bge-m3
    D:\\bge-m3-env\\Scripts\\python.exe backend\\scripts\\embedding_server.py --model BAAI/bge-small-zh-v1.5

首次启动会自动下载权重到 HF 缓存目录（由 HF_HOME 决定），之后从本地读取。启动后本服务
只监听 127.0.0.1，不对外暴露；模型权重和推理全部在本机完成，符合私有化要求。

本项目按 CPU 推理部署，不需要显卡或 CUDA，`--device` 会自动落到 CPU。CPU 下不要传
`--fp16`：该参数只在 GPU 上有效。只有知识库规模大幅增长或需要频繁重建时，才需要改用 GPU。

依赖与后端运行时隔离：torch 等推理依赖装在独立的环境里（见
backend/scripts/setup_embedding_server.ps1），后端 requirements.txt 不受影响。
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

LOGGER = logging.getLogger("embedding-server")

# bge-m3 是 M3 系列，dense 向量需要取 CLS 表示；其它 BGE 模型走普通 dense 输出。
M3_MODEL_MARKER = "m3"

# 请求模型必须定义在模块作用域：FastAPI 通过类型提示解析请求体，若模型定义在函数内部，
# 它无法解析该注解，会把参数当成查询参数并返回 422。
from pydantic import BaseModel  # noqa: E402


class EmbeddingRequest(BaseModel):
    """OpenAI 兼容请求体；input 允许单条字符串或字符串数组。"""

    input: str | list[str]
    model: str | None = None
    encoding_format: str | None = None


def _l2_normalize(vector: list[float]) -> list[float]:
    """把向量缩放到单位长度；零向量原样返回，避免除零。

    归一化在服务侧统一完成，应用侧不必再关心模型是否自带归一化，Milvus 用 COSINE 距离即可。
    """
    norm = sum(value * value for value in vector) ** 0.5
    if norm == 0:
        return vector
    return [value / norm for value in vector]


class EmbeddingBackend:
    """把 FlagEmbedding 的模型包装成可批量调用的 dense 向量编码器。

    维度不在这里写死：真实维度由第一次编码的返回值决定，并通过 `/health` 暴露，应用侧的
    `EmbeddingClient` 会拿实际维度与配置比对，不匹配就直接报错。
    """

    def __init__(
        self,
        model_name: str,
        device: str | None = None,
        use_fp16: bool = False,
        max_length: int | None = None,
    ) -> None:
        self.model_name = model_name
        self.device = device
        self.use_fp16 = use_fp16
        self.max_length = max_length
        self.dimension: int | None = None
        self._model = self._load(model_name)

    def _load(self, model_name: str) -> Any:
        """加载模型权重；M3 与普通 BGE 用不同的类，但对外接口一致。"""
        from FlagEmbedding import BGEM3FlagModel, FlagModel

        is_m3 = M3_MODEL_MARKER in model_name.lower()
        kwargs: dict[str, Any] = {}
        if self.device:
            kwargs["devices"] = self.device
        if is_m3:
            # return_dense=False：这里只提供 dense 向量，稀疏与多向量能力本阶段不用。
            kwargs["use_fp16"] = self.use_fp16
            LOGGER.info("加载 BGE-M3 模型：%s（fp16=%s）", model_name, self.use_fp16)
            return BGEM3FlagModel(model_name, **kwargs)
        LOGGER.info("加载 BGE 模型：%s", model_name)
        return FlagModel(model_name, **kwargs)

    def encode(self, texts: list[str], batch_size: int) -> list[list[float]]:
        """计算 dense 向量，返回与输入等长的普通 Python 列表。

        向量统一做 L2 归一化，配合 Milvus 的 COSINE 距离使用：归一化后余弦相似度等价于内积，
        不同模型的向量尺度差异也不会影响检索排序。批大小只影响显存/内存占用，不影响单条结果。
        """
        if not texts:
            return []
        is_m3 = M3_MODEL_MARKER in self.model_name.lower()
        if is_m3:
            # M3 的 dense 输出需要显式取 dense_vecs；稀疏与多向量能力本阶段不用。
            output = self._model.encode(
                texts,
                batch_size=batch_size,
                max_length=self.max_length or 8192,
                return_dense=True,
                return_sparse=False,
                return_colbert_vecs=False,
            )
            vectors = output["dense_vecs"]
        else:
            # FlagModel.encode 会把未知关键字继续传给 tokenizer，因此归一化必须自己算，
            # 不能通过 normalize_embeddings 参数传进去。
            vectors = self._model.encode(
                texts,
                batch_size=batch_size,
                max_length=self.max_length or 512,
            )
        result = [_l2_normalize([float(value) for value in vector]) for vector in vectors]
        if result and self.dimension is None:
            self.dimension = len(result[0])
            LOGGER.info("实际向量维度：%d", self.dimension)
        return result


def build_app(backend: EmbeddingBackend) -> Any:
    """构造 FastAPI 应用，暴露 OpenAI 兼容的 embeddings 接口。"""
    from fastapi import FastAPI

    app = FastAPI(title="Aiden local embedding server", version="1.0")

    @app.get("/health")
    def health() -> dict[str, Any]:
        """健康检查；同时返回已探测到的维度，便于脚本快速判断。"""
        return {"status": "ok", "model": backend.model_name, "dimension": backend.dimension}

    @app.get("/v1/models")
    def list_models() -> dict[str, Any]:
        """OpenAI 兼容的模型列表；部分客户端启动时会先探测它。"""
        return {
            "object": "list",
            "data": [{"id": backend.model_name, "object": "model", "owned_by": "local"}],
        }

    @app.post("/v1/embeddings")
    def create_embeddings(request: EmbeddingRequest) -> dict[str, Any]:
        """返回 dense 向量。

        响应结构与 OpenAI 一致：`data[i].index` 必须与请求顺序对应，应用侧依赖该字段排序，
        因此这里显式写入序号，不做并行乱序返回。
        """
        texts = [request.input] if isinstance(request.input, str) else list(request.input)
        vectors = backend.encode(texts, batch_size=app.state.batch_size)
        return {
            "object": "list",
            "model": request.model or backend.model_name,
            "data": [
                {"object": "embedding", "index": index, "embedding": vector}
                for index, vector in enumerate(vectors)
            ],
            "usage": {"prompt_tokens": sum(len(text) for text in texts), "total_tokens": 0},
        }

    app.state.backend = backend
    app.state.batch_size = 8
    return app


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析启动参数。"""
    parser = argparse.ArgumentParser(description="本地 OpenAI 兼容 embedding 服务")
    parser.add_argument("--model", default="BAAI/bge-m3", help="模型名或本地权重路径")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认仅本机")
    parser.add_argument("--port", type=int, default=8001, help="监听端口，默认 8001")
    parser.add_argument("--device", default=None, help="推理设备，如 cpu 或 cuda:0；默认自动（本项目为 CPU）")
    parser.add_argument("--fp16", action="store_true", help="fp16 推理，仅 GPU 有效；CPU 部署不要使用")
    parser.add_argument("--max-length", type=int, default=None, help="单条文本最大 token 数")
    parser.add_argument("--batch-size", type=int, default=8, help="单次编码的批大小")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """启动服务。"""
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    import uvicorn

    backend = EmbeddingBackend(
        model_name=args.model,
        device=args.device,
        use_fp16=args.fp16,
        max_length=args.max_length,
    )
    app = build_app(backend)
    app.state.batch_size = args.batch_size
    LOGGER.info("服务启动：http://%s:%d/v1/embeddings（模型 %s）", args.host, args.port, args.model)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
