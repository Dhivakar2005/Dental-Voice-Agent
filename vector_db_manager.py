import os
import requests
import numpy as np
from pymongo import MongoClient
import certifi
from dotenv import load_dotenv
from typing import List, Dict, Any

import structlog

load_dotenv()

logger = structlog.get_logger(__name__)

class OpenAIEmbeddingFunction:
    """Connects to OpenAI Cloud Embeddings."""
    def __init__(self):
        import openai
        # Fallback to DEEPGRAM_API_KEY if needed, though usually OpenAI key is preferred for embeddings
        api_key = os.getenv("DEEPGRAM_API_KEY")
        deepgram_base = os.getenv("DEEPGRAM_BASE_URL", "https://api.deepgram.com")
        self.client = openai.OpenAI(api_key=api_key, base_url=f"{deepgram_base}/v1/openai")

    def get_embedding(self, text: str) -> List[float]:
        try:
            resp = self.client.embeddings.create(
                input=text,
                model="text-embedding-3-small"
            )
            return resp.data[0].embedding
        except Exception as e:
            logger.error("openai_embedding_error", error=str(e))
            return [0.0] * 1536  # Correct dimension size for OpenAI

class VectorDBManager:
    _instance = None # Singleton

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(VectorDBManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized: return
        
        # MongoDB Configuration
        self.mongo_uri = os.getenv("MONGO_URI")
        self.db_name   = "dental_assistant"
        self.col_name  = "vector_faq"
        
        try:
            self.client = MongoClient(
                self.mongo_uri,
                tls=True,
                tlsCAFile=certifi.where(),
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=5000
            )
            self.db         = self.client[self.db_name]
            self.collection = self.db[self.col_name]
        except Exception as e:
            logger.error("vector_db_mongo_connection_error", error=str(e))
            from database_manager import SafeDatabase, SafeCollection
            self.client = None
            self.db = SafeDatabase()
            self.collection = SafeCollection(self.col_name)
        
        self.embedding_fn = OpenAIEmbeddingFunction()
        self._embedding_cache = {} # Simple LRU-style cache
        self._initialized = True

    def _get_cached_embedding(self, text: str) -> List[float]:
        """Simple cache wrapper for embeddings."""
        if text in self._embedding_cache:
            return self._embedding_cache[text]
        
        emb = self.embedding_fn.get_embedding(text)
        # Keep cache small (last 50 queries)
        if len(self._embedding_cache) > 50:
            self._embedding_cache.pop(next(iter(self._embedding_cache)))
        self._embedding_cache[text] = emb
        return emb

    def add_documents(self, documents: List[str], metadatas: List[Dict[str, Any]], ids: List[str]):
        """Store documents and their embeddings in MongoDB."""
        mongo_docs = []
        for i in range(len(ids)):
            emb = self.embedding_fn.get_embedding(documents[i])
            mongo_docs.append({
                "id": ids[i],
                "text": documents[i],
                "metadata": metadatas[i] if metadatas else {},
                "embedding": emb,
                "embedding_dim": len(emb)
            })
        
        if mongo_docs:
            self.collection.insert_many(mongo_docs)
            logger.info("added_documents_to_mongodb", num_docs=len(mongo_docs))

    def query(self, text: str, n_results: int = 3) -> Dict[str, Any]:
        """Performs a vector search using Optimized NumPy Cosine Similarity."""
        query_emb = self._get_cached_embedding(text)
        
        # Fetch all records from MongoDB (with embeddings)
        cursor = self.collection.find({}, {"text": 1, "embedding": 1})
        all_records = list(cursor)
        if not all_records:
            return {"documents": [[]]}

        # 1. Extract texts and embeddings into arrays
        texts = [rec["text"] for rec in all_records if rec.get("embedding")]
        embs  = np.array([rec["embedding"] for rec in all_records if rec.get("embedding")])
        
        if len(embs) == 0:
            return {"documents": [[]]}

        # Dynamically verify and match dimensions to avoid alignment crash
        db_dim = embs.shape[1]
        query_dim = len(query_emb)
        if query_dim != db_dim:
            logger.warning("embedding_dimension_mismatch", query_dim=query_dim, db_dim=db_dim)
            if query_dim < db_dim:
                query_emb = query_emb + [0.0] * (db_dim - query_dim)
            else:
                query_emb = query_emb[:db_dim]

        # 2. Vectorized Cosine Similarity
        dot_products = np.dot(embs, query_emb)
        norm_query   = np.linalg.norm(query_emb)
        norm_targets = np.linalg.norm(embs, axis=1)
        
        # Avoid division by zero
        scores = dot_products / (norm_query * norm_targets + 1e-9)

        # 3. Sort and pick top results
        top_indices = np.argsort(scores)[::-1][:n_results]
        top_docs    = [texts[i] for i in top_indices]
        
        return {"documents": [top_docs]}

    def get_context(self, text: str, n_results: int = 3) -> str:
        """Helper to get a flat string of context for the LLM."""
        try:
            results = self.query(text, n_results=n_results)
            docs = results.get("documents", [[]])[0]
            if not docs: return ""
            
            context_parts = []
            for i, doc in enumerate(docs):
                context_parts.append(f"Result {i+1}: {doc}")
            return "\n\n".join(context_parts)
        except Exception as e:
            logger.error("context_retrieval_failed", error=str(e))
            return ""

if __name__ == "__main__":
    vdb = VectorDBManager()
    logger.info("vector_db_initialized")
