# 全局配置：统一从 .env 读取环境变量，敏感信息仅存服务端
import os
from dotenv import load_dotenv
from pathlib import Path

# 定位项目根目录并加载 .env（递归查找，便于任意目录启动）
PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")


class Settings:
    """集中管理环境变量配置，避免散落的 os.getenv 调用"""

    # ---- MySQL ----
    MYSQL_HOST: str = os.getenv("MYSQL_HOST", "localhost")
    MYSQL_PORT: int = int(os.getenv("MYSQL_PORT", "3306"))
    MYSQL_USER: str = os.getenv("MYSQL_USER", "root")
    MYSQL_PASSWORD: str = os.getenv("MYSQL_PASSWORD", "")
    MYSQL_DATABASE: str = os.getenv("MYSQL_DATABASE", "literature_radar")

    # ---- JWT ----
    JWT_SECRET_KEY: str = os.getenv("JWT_SECRET_KEY", "insecure-default-secret")
    JWT_ALGORITHM: str = os.getenv("JWT_ALGORITHM", "HS256")
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = int(os.getenv("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))

    # ---- 管理员初始化（首次启动无 admin 时创建） ----
    ADMIN_USERNAME: str = os.getenv("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD: str = os.getenv("ADMIN_PASSWORD", "admin123")

    # ---- LLM 引导（模型池为空时写入默认模型；OpenAI 兼容接口） ----
    LLM_API_KEY: str = os.getenv("LLM_API_KEY", "")
    LLM_BASE_URL: str = os.getenv("LLM_BASE_URL", "")
    LLM_MODEL_NAME: str = os.getenv("LLM_MODEL_NAME", "glm-5.2")

    # ---- 外部检索 ----
    TAVILY_API_KEY: str = os.getenv("TAVILY_API_KEY", "")
    S2_API_KEY: str = os.getenv("S2_API_KEY", "")


settings = Settings()
