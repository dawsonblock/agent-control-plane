// Memory search component — search Graphiti temporal memory.

import { useState } from "react";
import { api, type MemoryFact } from "./api";

export function MemorySearch() {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<MemoryFact[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const handleSearch = async () => {
    if (!query.trim()) return;
    setLoading(true);
    setError("");
    try {
      const facts = await api.searchMemory(query);
      setResults(facts);
    } catch (e) {
      setError((e as Error).message);
      setResults([]);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="memory-search">
      <h3>Memory Search</h3>
      <div className="search-bar">
        <input
          type="text"
          placeholder="Search temporal memory..."
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && handleSearch()}
          className="search-input"
        />
        <button onClick={handleSearch} disabled={loading}>
          {loading ? "Searching..." : "Search"}
        </button>
      </div>
      {error && <div className="error">{error}</div>}
      {results.length > 0 && (
        <div className="memory-results">
          {results.map((fact, i) => (
            <div key={i} className="memory-fact">
              <div className="fact-text">{fact.fact}</div>
              <div className="fact-meta">
                {fact.source_node} → {fact.target_node}
                {fact.valid_at && ` · ${fact.valid_at}`}
              </div>
            </div>
          ))}
        </div>
      )}
      {results.length === 0 && !loading && query && (
        <div className="empty">No facts found.</div>
      )}
    </div>
  );
}
