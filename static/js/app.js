document.addEventListener('DOMContentLoaded', () => {
  // 모든 toast 자동 표시
  document.querySelectorAll('.toast').forEach(el => {
    new bootstrap.Toast(el).show();
  });
});
