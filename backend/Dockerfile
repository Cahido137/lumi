# 使用python-slim版
FROM python:3.12-slim

WORKDIR /app

# 使用pip安装uv
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ uv==0.12.9

# 预编译字节码
ENV UV_COMPILE_BYTECODE=1 PYTHONUNBUFFERED=1 UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple/

# 拷贝依赖清单
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

# 拷贝项目代码
COPY . .

# 赋予脚本执行权限
COPY --chmod=755 entrypoint.sh ./

# 使用非root身份运行
RUN useradd -m appuser && chown -R appuser /app
USER appuser

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]