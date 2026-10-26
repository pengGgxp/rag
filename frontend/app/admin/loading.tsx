export default function AdminLoading() {
  return (
    <div className="admin-skeleton" role="status" aria-label="加载中">
      {Array.from({ length: 5 }, (_, i) => (
        <span className="auth-skeleton" key={i} />
      ))}
    </div>
  );
}
