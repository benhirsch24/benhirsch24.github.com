document.addEventListener('DOMContentLoaded', () => {
  const toc = document.querySelector('.post-toc');
  if (!toc) return;

  const headings = Array.from(
    document.querySelectorAll('.post-content h2[id], .post-content h3[id], .post-content h4[id]')
  );
  if (!headings.length) return;

  const escapeId = (id) => {
    if (window.CSS && typeof window.CSS.escape === 'function') {
      return window.CSS.escape(id);
    }
    return id;
  };

  const linkMap = new Map();
  headings.forEach((heading) => {
    const link = toc.querySelector(`a[href="#${escapeId(heading.id)}"]`);
    if (link) {
      linkMap.set(heading, link);
    }
  });

  if (!linkMap.size) return;

  let activeLink = null;
  const setActiveLink = (link) => {
    if (activeLink === link) return;

    if (activeLink) {
      const previousItem = activeLink.closest('li');
      if (previousItem) previousItem.classList.remove('active');
    }

    activeLink = link;

    if (activeLink) {
      const nextItem = activeLink.closest('li');
      if (nextItem) nextItem.classList.add('active');
    }
  };

  const observer = new IntersectionObserver(
    (entries) => {
      const visible = entries
        .filter((entry) => entry.isIntersecting)
        .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);

      if (visible.length) {
        const topEntry = visible[0];
        const link = linkMap.get(topEntry.target);
        if (link) setActiveLink(link);
      }
    },
    {
      rootMargin: '0px 0px -40% 0px',
      threshold: [0, 0.1, 0.25, 0.5],
    }
  );

  headings.forEach((heading) => observer.observe(heading));
  const firstLink = linkMap.get(headings[0]) || null;
  setActiveLink(firstLink);
});
