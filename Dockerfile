# stock-tracker — 零依赖 Python 标准库后端 + 静态前端
# 运行时仅依赖 Python 3.13 标准库，无需 pip install 任何第三方包。
FROM python:3.13-slim

WORKDIR /app

# 复制依赖清单（运行时为空，仅用于平台识别 Python 项目）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt || true

# 复制源码与配置（data/ 已被 .gitignore 排除，不会进入构建上下文）
COPY . .

# 确保运行时数据目录存在（应用启动也会自建）
RUN mkdir -p data

# 应用读取环境变量 PORT（Render 注入），默认 8080
EXPOSE 8080

CMD ["python", "-m", "stock_tracker"]
