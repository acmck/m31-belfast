// The red-light toggle. The initial state is applied by an inline script in
// <head> before first paint; this only wires up the button.
const KEY = 'red-light';
const root = document.documentElement;

function apply(on: boolean, save: boolean): void {
  root.dataset.light = on ? 'red' : 'normal';
  document.querySelectorAll<HTMLButtonElement>('[data-red-toggle]').forEach((b) => {
    b.setAttribute('aria-pressed', String(on));
  });
  const meta = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]');
  if (meta) meta.content = on ? '#000000' : '#211f1c';
  if (save) {
    try {
      localStorage.setItem(KEY, on ? '1' : '0');
    } catch {
      /* private mode: fine, it just won't be remembered */
    }
  }
}

apply(root.dataset.light === 'red', false);
document.querySelectorAll<HTMLButtonElement>('[data-red-toggle]').forEach((b) => {
  b.addEventListener('click', () => apply(root.dataset.light !== 'red', true));
});
