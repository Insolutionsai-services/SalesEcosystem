"""TF-IDF nearest-neighbour index over short texts. Fit once at training time, query in about a millisecond.
Character n-grams cope with typos, pack sizes ("20L") and mixed-script text without any external model."""
from sklearn.feature_extraction.text import TfidfVectorizer


class TextIndex:
    def __init__(self, keys: list, docs: list[str]):
        self.keys = list(keys)
        self.vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4), lowercase=True, sublinear_tf=True)
        self.matrix = self.vectorizer.fit_transform(docs) if docs else None

    def __len__(self) -> int:
        return len(self.keys)

    def search(self, query: str, k: int = 5, min_score: float = 0.05) -> list[tuple[object, float]]:
        """[(key, cosine similarity)] best first. Rows are L2-normalised, so a dot product is the cosine."""
        if self.matrix is None or not query.strip():
            return []
        sims = (self.matrix @ self.vectorizer.transform([query]).T).toarray().ravel()
        top = sims.argsort()[::-1][:k]
        return [(self.keys[i], float(sims[i])) for i in top if sims[i] > min_score]

    def best(self, query: str, min_score: float, min_margin: float) -> tuple[object, float] | None:
        """The single confident match, or None if nothing clears min_score or the top two are too close to call."""
        hits = self.search(query, k=2, min_score=0)
        if not hits or hits[0][1] < min_score:
            return None
        if len(hits) > 1 and hits[0][1] - hits[1][1] < min_margin:
            return None
        return hits[0]
