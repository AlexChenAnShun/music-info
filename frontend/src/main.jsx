import React from "react";
import { createRoot } from "react-dom/client";
import { AppProvider } from "./AppContext";
import App from "./App";
import "./base.css";
import "./organizer.css";
import "./app.css";
class Boundary extends React.Component {
  state = { error: null };
  static getDerivedStateFromError(error) {
    return { error };
  }
  render() {
    return this.state.error ? (
      <main className="page">
        <h1>Music Info</h1>
        <p role="alert">
          页面加载失败，请刷新重试。此错误不会自动提交文件修改。
        </p>
        <button className="btn" onClick={() => location.reload()}>
          重新加载
        </button>
      </main>
    ) : (
      this.props.children
    );
  }
}
createRoot(document.getElementById("root")).render(
  <Boundary>
    <AppProvider>
      <App />
    </AppProvider>
  </Boundary>,
);
