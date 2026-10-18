'use strict';

function caseFoldFor(el) {
  const article = el && el.closest ? el.closest('article.case') : null;
  return article ? article.querySelector(':scope > details.case-fold') : null;
}

function revealFragment(scroll) {
  const id = window.location.hash.slice(1);
  if (!id) return;
  const target = document.getElementById(id);
  if (!target) return;
  let revealed = false;
  const fold = target.matches && target.matches('article.case') ? target.querySelector(':scope > details.case-fold') : null;
  if (fold && !fold.open) { fold.open = true; revealed = true; }
  for (let parent = target.parentElement; parent; parent = parent.parentElement) {
    if (parent.tagName === 'DETAILS' && !parent.open) {
      parent.open = true;
      revealed = true;
    }
  }
  if (revealed || scroll) requestAnimationFrame(() => target.scrollIntoView({ block: 'start', behavior: 'instant' }));
  syncFoldAll();
}

function allFolds() { return Array.from(document.querySelectorAll('details.case-fold')); }

function syncFoldAll() {
  const btn = document.querySelector('.fold-all');
  if (!btn) return;
  const folds = allFolds();
  const allOpen = folds.length > 0 && folds.every(d => d.open);
  btn.dataset.mode = allOpen ? 'close' : 'open';
  btn.textContent = allOpen ? '모든 프로젝트 접기' : '모든 프로젝트 펼치기';
}

function setupFolds() {
  const btn = document.querySelector('.fold-all');
  if (btn) {
    btn.addEventListener('click', () => {
      const open = btn.dataset.mode !== 'close';
      allFolds().forEach(d => { d.open = open; });
      syncFoldAll();
    });
  }
  allFolds().forEach(d => d.addEventListener('toggle', syncFoldAll));
  document.querySelectorAll('.fold-close').forEach(b => {
    b.addEventListener('click', () => {
      const article = document.getElementById(b.dataset.case);
      const fold = article ? article.querySelector(':scope > details.case-fold') : null;
      if (fold) fold.open = false;
      if (article) article.scrollIntoView({ block: 'start', behavior: 'instant' });
      syncFoldAll();
    });
  });
  document.addEventListener('click', (ev) => {
    const a = ev.target.closest ? ev.target.closest('a[href^="#"]') : null;
    if (!a) return;
    const id = a.getAttribute('href').slice(1);
    if (!id) return;
    const target = document.getElementById(id);
    if (!target) return;
    // same-hash clicks do not fire hashchange; open the target's fold explicitly
    setTimeout(() => { revealFragment(true); }, 0);
  });
  let printState = null;
  window.addEventListener('beforeprint', () => {
    printState = allFolds().map(d => d.open);
    allFolds().forEach(d => { d.open = true; });
  });
  window.addEventListener('afterprint', () => {
    if (!printState) return;
    allFolds().forEach((d, i) => { d.open = printState[i]; });
    printState = null;
    syncFoldAll();
  });
  syncFoldAll();
}

window.addEventListener('hashchange', () => revealFragment(false));
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => { setupFolds(); revealFragment(false); });
} else {
  setupFolds(); revealFragment(false);
}
