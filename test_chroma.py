import os
import chromadb
class DummyEmbeddingFunction:
    def name(self):
        return "dummy_func"
    def __call__(self, input: list) -> list:
        return [[0.0] * 128 for _ in input]

db_path = "data/chroma_db_test"
client = chromadb.PersistentClient(path=db_path)
client.get_or_create_collection("personas_test", embedding_function=DummyEmbeddingFunction())
print("Works!")
