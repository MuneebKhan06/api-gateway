import { Component, type ErrorInfo, type ReactNode } from "react";

interface ErrorBoundaryProps {
  children: ReactNode;
  /** Shown above the message, e.g. which part of the console failed. */
  where?: string;
}

interface ErrorBoundaryState {
  error: Error | null;
}

/**
 * Without this, one rendering error blanks the whole console mid demo. With
 * it, the page says what broke and how to get back, and the rest of the
 * layout keeps working.
 */
export default class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("Console page crashed", error, info.componentStack);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;

    return (
      <div className="card stack" role="alert">
        <h2>{this.props.where ?? "This page"} hit an error</h2>
        <p className="hint" style={{ margin: 0 }}>
          The gateway itself is unaffected; this is the console failing to draw. Other pages in the
          sidebar still work.
        </p>
        <pre className="code">{error.message}</pre>
        <div className="row">
          <button type="button" className="primary" onClick={() => window.location.reload()}>
            Reload
          </button>
          <button type="button" onClick={() => this.setState({ error: null })}>
            Try again
          </button>
        </div>
      </div>
    );
  }
}
