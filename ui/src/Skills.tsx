import { useState, useEffect } from "react";
import { api, type SkillSummary } from "./api";

export function Skills() {
  const [skills, setSkills] = useState<SkillSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);

  useEffect(() => {
    async function load() {
      try {
        setLoading(true);
        const data = await api.listSkills();
        setSkills(data);
        setError(null);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load skills");
      } finally {
        setLoading(false);
      }
    }
    load();
  }, []);

  if (loading) return <div className="loading">Loading skills...</div>;
  if (error) return <div className="error">{error}</div>;
  if (skills.length === 0) {
    return (
      <div className="empty">
        No skills found. Add YAML playbooks to the <code>skills/</code> directory.
      </div>
    );
  }

  return (
    <div className="skills-view">
      <h2>Skills Governance</h2>
      <p className="skills-description">
        Active YAML playbooks that define review gates, hard blocks, and
        risk elevators for specific task types.
      </p>
      <div className="skills-list">
        {skills.map((skill) => (
          <div
            key={skill.name}
            className={`skill-card ${expanded === skill.name ? "expanded" : ""}`}
            onClick={() => setExpanded(expanded === skill.name ? null : skill.name)}
          >
            <div className="skill-header">
              <h3>{skill.name}</h3>
              <div className="skill-badges">
                {skill.has_hard_blocks && (
                  <span className="badge badge-hard-block">Hard Blocks</span>
                )}
                {skill.has_risk_elevators && (
                  <span className="badge badge-risk-elevator">Risk Elevators</span>
                )}
              </div>
            </div>
            <p className="skill-purpose">{skill.purpose}</p>
            {expanded === skill.name && skill.rules.length > 0 && (
              <div className="skill-rules">
                <h4>Rules</h4>
                <ul>
                  {skill.rules.map((rule, idx) => (
                    <li key={idx}>{rule}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
