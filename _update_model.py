# 一次性脚本：把模型池默认模型配置改成当前环境所需（ORM 版）
import os

from sqlalchemy import select, update

from app.models import LlmModel
from app.session import SessionLocal

with SessionLocal() as session:
    n = session.execute(
        update(LlmModel).where(LlmModel.is_default.is_(True)).values(
            name="qwen3.7-plus (默认)", model_id="qwen3.7-plus", vendor="dashscope",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key_enc=os.getenv("LLM_API_KEY", ""),
        )).rowcount
    session.commit()
    print("updated models:", n)
    for row in session.scalars(select(LlmModel)).all():
        print({"id": row.id, "name": row.name, "model_id": row.model_id,
               "vendor": row.vendor, "status": row.status, "base_url": row.base_url})
