export default function ComingSoon({ title, summary }: { title: string; summary: string }) {
  return (
    <>
      <div className="page-header">
        <h1>{title}</h1>
        <p>{summary}</p>
      </div>
      <div className="card empty">This page is being built.</div>
    </>
  );
}
