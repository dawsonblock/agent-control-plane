import { useState } from "react";
import { TaskList } from "./TaskList";
import { TaskDetail } from "./TaskDetail";
import { RunForm } from "./RunForm";
import { MemorySearch } from "./MemorySearch";
import "./App.css";

function App() {
  const [selectedTask, setSelectedTask] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [view, setView] = useState<"tasks" | "memory">("tasks");

  const refresh = () => setRefreshKey((k) => k + 1);

  const handleSubmitted = (taskId: string) => {
    refresh();
    setSelectedTask(taskId);
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
            className={view === "memory" ? "active" : ""}
            onClick={() => setView("memory")}
          >
            Memory
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
        {view === "memory" && <MemorySearch />}
      </main>
    </div>
  );
}

export default App;
