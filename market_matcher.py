"""Smart market matching with caching and multi-tier fuzzy matching."""
import json
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict
from datetime import datetime

try:
    from fuzzywuzzy import fuzz
except ImportError:
    fuzz = None

try:
    from sentence_transformers import SentenceTransformer
    import numpy as np
except ImportError:
    SentenceTransformer = None
    np = None


@dataclass
class MatchDatabase:
    """Persistent cache of market matches."""
    matched: Dict[str, str]  # poly_id -> kalshi_ticker
    unmatched_poly: List[str]
    unmatched_kalshi: List[str]
    last_full_scan: str
    
    def save(self, path: Path):
        with open(path, 'w') as f:
            json.dump(asdict(self), f, indent=2)
    
    @classmethod
    def load(cls, path: Path) -> 'MatchDatabase':
        if path.exists():
            with open(path) as f:
                data = json.load(f)
            return cls(**data)
        return cls(
            matched={},
            unmatched_poly=[],
            unmatched_kalshi=[],
            last_full_scan=""
        )


class MarketMatcher:
    """Multi-tier fuzzy matching with caching."""
    
    FUZZY_THRESHOLD = 70
    PARTIAL_THRESHOLD = 75
    SEMANTIC_THRESHOLD = 0.85
    REMATCH_INTERVAL = 300  # 5 minutes
    
    def __init__(self, cache_path: str = "match_cache.json"):
        self.cache_path = Path(cache_path)
        self.db = MatchDatabase.load(self.cache_path)
        self.embedding_model = None
        self._embeddings_cache: Dict[str, list] = {}
        
        # Track market titles for matching
        self.poly_titles: Dict[str, str] = {}  # id -> title
        self.kalshi_titles: Dict[str, str] = {}  # ticker -> title
    
    def _load_embedding_model(self):
        """Lazy load the embedding model."""
        if self.embedding_model is None and SentenceTransformer is not None:
            print("  Loading semantic embedding model...")
            self.embedding_model = SentenceTransformer('all-MiniLM-L6-v2')
    
    def _get_embedding(self, text: str) -> Optional[list]:
        """Get embedding for text, with caching."""
        if text in self._embeddings_cache:
            return self._embeddings_cache[text]
        
        self._load_embedding_model()
        if self.embedding_model is None:
            return None
        
        embedding = self.embedding_model.encode(text).tolist()
        self._embeddings_cache[text] = embedding
        return embedding
    
    def _cosine_similarity(self, a: list, b: list) -> float:
        """Calculate cosine similarity between two vectors."""
        if np is None:
            return 0.0
        a, b = np.array(a), np.array(b)
        return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))
    
    def _fuzzy_match(self, poly_title: str, kalshi_title: str) -> Tuple[bool, str]:
        """Run the 4-tier matching cascade. Returns (matched, method)."""
        if fuzz is None:
            return False, "no_fuzzywuzzy"
        
        poly_lower = poly_title.lower()
        kalshi_lower = kalshi_title.lower()
        
        # Tier 1: Token Sort Ratio
        score = fuzz.token_sort_ratio(poly_lower, kalshi_lower)
        if score >= self.FUZZY_THRESHOLD:
            return True, f"token_sort_{score}"
        
        # Tier 2: Token Set Ratio
        score = fuzz.token_set_ratio(poly_lower, kalshi_lower)
        if score >= self.FUZZY_THRESHOLD:
            return True, f"token_set_{score}"
        
        # Tier 3: Partial Ratio
        score = fuzz.partial_ratio(poly_lower, kalshi_lower)
        if score >= self.PARTIAL_THRESHOLD:
            return True, f"partial_{score}"
        
        # Tier 4: Semantic Embedding (only if others fail)
        poly_emb = self._get_embedding(poly_lower)
        kalshi_emb = self._get_embedding(kalshi_lower)
        
        if poly_emb and kalshi_emb:
            similarity = self._cosine_similarity(poly_emb, kalshi_emb)
            if similarity >= self.SEMANTIC_THRESHOLD:
                return True, f"semantic_{similarity:.2f}"
        
        return False, "no_match"
    
    def update_markets(self, poly_markets: List[dict], kalshi_markets: List[dict]):
        """Update internal title maps with current market data."""
        self.poly_titles = {
            m.get('condition_id', ''): m.get('question', '')
            for m in poly_markets if m.get('condition_id')
        }
        self.kalshi_titles = {
            m.get('ticker', ''): m.get('title', '')
            for m in kalshi_markets if m.get('ticker')
        }
    
    def get_match(self, poly_id: str) -> Optional[str]:
        """Get cached match for a Polymarket ID. Returns Kalshi ticker or None."""
        return self.db.matched.get(poly_id)
    
    def match_new_market(self, poly_id: str, poly_title: str) -> Optional[str]:
        """Try to match a new Polymarket market. Returns Kalshi ticker or None."""
        # Already matched?
        if poly_id in self.db.matched:
            return self.db.matched[poly_id]
        
        # Already known unmatched?
        if poly_id in self.db.unmatched_poly:
            return None
        
        # Try to find a match
        best_match = None
        best_method = None
        
        for kalshi_ticker, kalshi_title in self.kalshi_titles.items():
            # Skip already matched Kalshi markets
            if kalshi_ticker in self.db.matched.values():
                continue
            
            matched, method = self._fuzzy_match(poly_title, kalshi_title)
            if matched:
                best_match = kalshi_ticker
                best_method = method
                break  # Take first match (could improve to find best)
        
        if best_match:
            self.db.matched[poly_id] = best_match
            print(f"    ✓ Matched: {poly_title[:40]}... → {best_match} ({best_method})")
            self._save()
            return best_match
        else:
            if poly_id not in self.db.unmatched_poly:
                self.db.unmatched_poly.append(poly_id)
                self._save()
            return None
    
    def should_rematch_unmatched(self) -> bool:
        """Check if it's time to re-run matching on unmatched markets."""
        if not self.db.last_full_scan:
            return True
        
        try:
            last = datetime.fromisoformat(self.db.last_full_scan)
            elapsed = (datetime.utcnow() - last).total_seconds()
            return elapsed >= self.REMATCH_INTERVAL
        except:
            return True
    
    def rematch_unmatched(self) -> int:
        """Re-run matching on all unmatched markets. Returns count of new matches."""
        print("  🔄 Re-matching unmatched markets...")
        new_matches = 0
        
        # Copy list since we'll modify it
        for poly_id in list(self.db.unmatched_poly):
            poly_title = self.poly_titles.get(poly_id, "")
            if not poly_title:
                continue
            
            for kalshi_ticker, kalshi_title in self.kalshi_titles.items():
                if kalshi_ticker in self.db.matched.values():
                    continue
                
                matched, method = self._fuzzy_match(poly_title, kalshi_title)
                if matched:
                    self.db.matched[poly_id] = kalshi_ticker
                    self.db.unmatched_poly.remove(poly_id)
                    print(f"    ✓ New match: {poly_title[:40]}... → {kalshi_ticker}")
                    new_matches += 1
                    break
        
        self.db.last_full_scan = datetime.utcnow().isoformat()
        self._save()
        return new_matches
    
    def _save(self):
        """Save database to disk."""
        self.db.save(self.cache_path)
    
    def get_all_matches(self) -> Dict[str, str]:
        """Get all matched pairs."""
        return self.db.matched.copy()
    
    def stats(self) -> dict:
        """Get matching statistics."""
        return {
            "matched_pairs": len(self.db.matched),
            "unmatched_poly": len(self.db.unmatched_poly),
            "unmatched_kalshi": len(self.db.unmatched_kalshi),
            "last_full_scan": self.db.last_full_scan
        }
