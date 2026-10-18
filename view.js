'use strict';

function revealFragment() {
  const target = document.getElementById(window.location.hash.slice(1));
  if (!target) return;
  let revealed = false;
  for (let parent = target.parentElement; parent; parent = parent.parentElement) {
    if (parent.tagName === 'DETAILS' && !parent.open) {
      parent.open = true;
      revealed = true;
    }
  }
  if (revealed) requestAnimationFrame(() => target.scrollIntoView({ block: 'start', behavior: 'instant' }));
}

window.addEventListener('hashchange', revealFragment);
revealFragment();
