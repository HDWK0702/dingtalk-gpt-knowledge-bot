# 这是一个最小 Embedding 连通性测试：只发送一句测试文本，确认 Key、地址和模型是否正确。
import os
from dotenv import load_dotenv
from openai import OpenAI

# 从项目 .env 读取 Embedding 配置；真实密钥不会写在代码中。
load_dotenv()

# 先检查密钥是否存在，避免后面得到不容易理解的接口认证错误。
embedding_key = os.getenv("EMBEDDING_API_KEY")
if not embedding_key:
    raise RuntimeError("请先在 .env 中填写 EMBEDDING_API_KEY")

# 组装 OpenAI 兼容客户端参数。Embedding 服务既可以是官方地址，也可以是兼容平台的地址。
client_options = {"api_key": embedding_key}

if embedding_base_url := os.getenv("EMBEDDING_BASE_URL"):
    client_options["base_url"] = embedding_base_url

# 创建客户端后发送一段示例问题，服务会返回“这段文字的数字坐标”。
client = OpenAI(**client_options)

response = client.embeddings.create(
    model=os.getenv("OPENAI_EMBEDDING_MODEL", "BAAI/bge-m3"),
    input="员工出差住宿费怎么报销？",
)

# 向量通常有几百或上千个数字；这里不打印全部，只看长度和前五个数字确认它确实返回了结果。
vector = response.data[0].embedding

print("向量生成成功")
print("向量长度：", len(vector))
print("前 5 个数字：", vector[:5])
