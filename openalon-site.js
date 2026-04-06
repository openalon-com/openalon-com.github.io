(function () {
  var hamburger = document.getElementById("hamburger");
  var menu = document.getElementById("header-menu");
  var header = document.getElementById("header");
  var background = document.getElementById("background-h");
  var shell = document.querySelector(".navbar-logo-left");

  function closeMenu() {
    if (!hamburger || !menu || !header || !background) return;
    document.body.classList.remove("scroll-lock");
    hamburger.classList.remove("is-active");
    menu.classList.remove("is-active");
    header.classList.remove("is-active");
    background.classList.remove("is-active");
  }

  function toggleMenu(event) {
    if (event) event.stopPropagation();
    if (!hamburger || !menu || !header || !background) return;
    var isOpen = menu.classList.contains("is-active");
    if (isOpen) {
      closeMenu();
      return;
    }
    document.body.classList.add("scroll-lock");
    hamburger.classList.add("is-active");
    menu.classList.add("is-active");
    header.classList.add("is-active");
    background.classList.add("is-active");
  }

  if (hamburger && menu && header && background) {
    hamburger.addEventListener("click", toggleMenu);
    background.addEventListener("click", closeMenu);
    menu.querySelectorAll("a").forEach(function (link) {
      link.addEventListener("click", closeMenu);
    });
    document.addEventListener("click", function (event) {
      if (!menu.classList.contains("is-active")) return;
      if (!shell || shell.contains(event.target)) return;
      closeMenu();
    });
    window.addEventListener("resize", function () {
      if (window.innerWidth > 767) {
        closeMenu();
      }
    });
  }

  var bannerKey = "openalon_cookie_banner_ack";
  var preferenceKey = "openalon_cookie_preferences";
  var banner = document.getElementById("cookie-banner");
  var acceptButton = document.getElementById("cookie-banner-accept");
  var declineButton = document.getElementById("cookie-banner-close");

  function showBanner() {
    if (!banner) return;
    banner.style.display = "block";
  }

  function hideBanner() {
    if (!banner) return;
    banner.style.display = "none";
  }

  function acknowledge(preferences) {
    localStorage.setItem(bannerKey, "1");
    if (preferences) {
      localStorage.setItem(preferenceKey, JSON.stringify(preferences));
    }
    hideBanner();
  }

  if (banner && !localStorage.getItem(bannerKey)) {
    showBanner();
  }

  if (acceptButton) {
    acceptButton.addEventListener("click", function () {
      acknowledge({ necessary: true, analytics: true, ads: false });
    });
  }

  if (declineButton) {
    declineButton.addEventListener("click", function () {
      acknowledge({ necessary: true, analytics: false, ads: false });
    });
  }

  document.querySelectorAll(".cookie-policy-link").forEach(function (trigger) {
    trigger.addEventListener("click", function (event) {
      event.preventDefault();
      showBanner();
    });
  });
})();
