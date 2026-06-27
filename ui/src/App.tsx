import { useState } from "react";
import { TaskList } from "./TaskList";
import { TaskDetail } from "./TaskDetail";
import { RunForm } from "./RunForm";
import { MemorySearch } from "./MemorySearch";
import { MissionList } from "./MissionList";
import { MissionDetail } from "./MissionDetail";
import { MissionForm } from "./MissionForm";
import { Skills } from "./Skills";
import "./App.css";

type View = "tasks" | "memory" | "missions" | "skills";

function App() {
  const [selectedTask, setSelectedTask] = useState<string | null>(null);
  const [selectedMission, setSelectedMission] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [view, setView] = useState<View>("tasks");

  const refresh = () => setRefreshKey((k) => k + 1);

  const handleSubmitted = (taskId: string) => {
    refresh();
    setSelectedTask(taskId);
  };

  const handleMissionCreated = (missionId: string) => {
    refresh();
    setSelectedMission(missionId);
  };

  return (
    <div className="app">
      <header className="app-header">
        <h1>Agent Control Plane</h1>
        <nav className="nav-tabs">
          <button
            className={view === "tasks" ? "active" : ""}
            onClick={() => setView("tasks")}
          >
            Tasks
          </button>
          <button
            className={view === "missions" ? "active" : ""}
            onClick={() => setView("missions")}
          >
            Missions
          </button>
          <button
            className={view === "memory" ? "active" : ""}
            onClick={() => setView("memory")}
          >
            Memory
          </button>
          <button
            className={view === "skills" ? "active" : ""}
            onClick={() => setView("skills")}
          >
            Skills
          </button>
        </nav>
      </header>

      <main className="app-main">
        {view === "tasks" && (
          <>
            <div className="sidebar">
              <RunForm onSubmitted={handleSubmitted} />
              <TaskList
                onSelect={setSelectedTask}
                selectedId={selectedTask}
                refreshKey={refreshKey}
              />
            </div>
            <div className="content">
              {selectedTask ? (
                <TaskDetail taskId={selectedTask} onAction={refresh} />
              ) : (
                <div className="empty">Select a task to view details</div>
              )}
            </div>
          </>
        )}
        {view === "missions" && (
          <>
            <div className="sidebar">
              <MissionForm onCreated={handleMissionCreated} />
              <MissionList
                onSelect={setSelectedMission}
                selectedId={selectedMission}
                refreshKey={refreshKey}
              />
            </div>
            <div className="content">
              {selectedMission ? (
                <MissionDetail missionId={selectedMission} />
              ) : (
                <div className="empty">Select a mission to view details</div>
              )}
            </div>
          </>
        )}
        {view === "memory" && <MemorySearch />}
        {view === "skills" && <Skills />}
      </main>
    </div>
  );
}

export default App;
