document.addEventListener("DOMContentLoaded", function () {
  var items = document.querySelectorAll(".reveal");

  // Older browsers without IntersectionObserver: just show everything
  if (!("IntersectionObserver" in window)) {
    items.forEach(function (el) { el.classList.add("is-visible"); });
    return;
  }

  var observer = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (entry.isIntersecting) {
        entry.target.classList.add("is-visible");
        observer.unobserve(entry.target); // animate only once
      }
    });
  }, {
    threshold: 0.15 // trigger when 15% of the element is visible
  });

  items.forEach(function (el) { observer.observe(el); });
});