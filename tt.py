import os
import litellm
r = litellm.rerank(
    model="nvidia_nim/nvidia/nv-rerankqa-mistral-4b-v3",
    query="which pets need grooming monthly?",
    documents=["Doodles need grooming every 4-6 weeks.", "Goldfish eat twice daily."],
    api_base="https://ai.api.nvidia.com/v1/retrieval/nvidia/reranking",
    api_key=os.environ["NVIDIA_NIM_API_KEY"],
)
