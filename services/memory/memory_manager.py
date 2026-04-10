import chromadb
import asyncio
import os
from typing import Optional, Dict, Any

class DummyEmbeddingFunction:
    """Mock embedding to avoid large file downloads during tests."""
    def name(self) -> str:
        return "dummy_embedding_function"

    def __call__(self, input: list) -> list:
        return [[0.0] * 128 for _ in input]

class PersonasMemory:
    """
    Manages employee profiles and visitor personas using ChromaDB.
    Enables person-aware greetings and contextual memory on-device.
    OPTIMIZED: Async support to avoid blocking dialogue loop.
    """
    def __init__(self, db_path: str = "data/chroma_db"):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.client = chromadb.PersistentClient(path=db_path)
        self.collection = self.client.get_or_create_collection(
            "personas", 
            embedding_function=DummyEmbeddingFunction()
        )

    def upsert_persona(self, person_id: str, profile: Dict[str, Any]):
        """Saves or updates an employee/visitor profile."""
        # A summary for lightweight semantic search if needed
        summary = f"Name: {profile.get('name', 'Unknown')}. Role: {profile.get('role', 'Visitor')}. " \
                  f"Department: {profile.get('dept', 'N/A')}. Preferences: {profile.get('pref', 'None')}."
        
        self.collection.upsert(
            documents=[summary],
            metadatas=[profile],
            ids=[person_id]
        )

    def get_persona(self, person_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves a persona by unique ID (e.g., from face recognition)."""
        result = self.collection.get(ids=[person_id])
        if result and result["metadatas"]:
            return result["metadatas"][0]
        return None
    
    async def get_persona_async(self, person_id: str) -> Optional[Dict[str, Any]]:
        """
        OPTIMIZATION: Async wrapper around get_persona.
        Runs blocking ChromaDB operation in thread pool to avoid blocking dialogue loop.
        """
        return await asyncio.to_thread(self.get_persona, person_id)

    def semantic_lookup(self, query: str) -> Optional[Dict[str, Any]]:
        """Finds the most relevant persona based on a text description."""
        results = self.collection.query(
            query_texts=[query],
            n_results=1
        )
        if results and results["metadatas"] and results["metadatas"][0]:
            return results["metadatas"][0][0]
        return None
