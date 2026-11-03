// Google Analytics 4, cookieless: Consent Mode v2 with every storage type denied, so gtag.js
// sets no cookies and sends only anonymous pings. The server includes this script (and the
// ga-id meta tag) on "/", "/privacy" and "/terms" only when GA_MEASUREMENT_ID is set, never on
// the download page, whose URL fragment holds the link token.
(() => {
  const meta = document.querySelector('meta[name="ga-id"]');
  const id = meta ? meta.content : "";
  if (!/^G-[A-Z0-9]{4,16}$/.test(id)) return;

  window.dataLayer = window.dataLayer || [];
  function gtag() { dataLayer.push(arguments); } // gtag.js expects the arguments object itself
  // Consent first: it must be in the queue before config, or the first hit may use storage.
  gtag("consent", "default", {
    analytics_storage: "denied",
    ad_storage: "denied",
    ad_user_data: "denied",
    ad_personalization: "denied",
  });
  gtag("js", new Date());
  // page_location without query or fragment, so nothing after the path ever reaches Google.
  // (GA4 never stores IP addresses, so there is no anonymize_ip to set.)
  gtag("config", id, { page_location: location.origin + location.pathname });

  const script = document.createElement("script");
  script.async = true;
  script.src = "https://www.googletagmanager.com/gtag/js?id=" + encodeURIComponent(id);
  document.head.appendChild(script);
})();
