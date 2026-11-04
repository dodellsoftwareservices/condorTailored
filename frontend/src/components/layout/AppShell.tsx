import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import {
  BookOpen,
  Bug,
  Eye,
  Maximize2,
  Minimize2,
  Moon,
  PanelLeft,
  Search,
  Sun,
} from "lucide-react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";

import { ChatBubble } from "@/components/chat/ChatBubble";
import { ConnectKeysOverlay } from "@/components/ConnectKeysOverlay";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { GuidePanel } from "@/components/guide/GuidePanel";
import { RelaunchBanner } from "@/components/RelaunchBanner";
import { ReportIssueDialog } from "@/components/ReportIssueDialog";
import { TelemetryConsentBanner } from "@/components/TelemetryConsentBanner";
import { ChatProvider } from "@/hooks/useChat";
import { useCredentials } from "@/hooks/useCredentials";
import { usePrefetchData } from "@/hooks/usePrefetchData";
import { useServer } from "@/hooks/useServer";
import { useShellLayout } from "@/hooks/useShellLayout";
import { useTheme } from "@/hooks/useTheme";
import { NAV_ITEMS, FULL_BLEED_ROUTES } from "@/lib/nav";
import { MOBILE_MEDIA_QUERY } from "@/lib/layout-scale";
import { routeFacts } from "@/lib/pageFacts";
import { GUIDE_OPEN_KEY } from "@/lib/sessionState";
import { useViewFacts } from "@/lib/viewFacts";
import { CommandPalette } from "./CommandPalette";
import { CurrencySelector } from "./CurrencySelector";
import { NotificationBell } from "./NotificationBell";
import { ServerSelector } from "./ServerSelector";

const FULL_BLEED_PATTERNS = [/^\/agents\/[^/]+$/];
const MOBILE_NAV = NAV_ITEMS.filter(({ to }) => ["/", "/portfolio", "/trade", "/bots"].includes(to));

export function AppShell() {
  return (
    <ChatProvider>
      <AppShellBody />
    </ChatProvider>
  );
}

function AppShellBody() {
  const { server } = useServer();
  const { pathname, search } = useLocation();
  const { theme, toggleTheme } = useTheme();
  const navigate = useNavigate();
  const { hasKeys, isLoading: keysLoading } = useCredentials();
  // The overlay must never cover the page it sends you to (/settings), nor the
  // pages that work without keys (/routines, and the chat home at /).
  const KEYS_EXEMPT_ROUTES = ["/agents", "/routines", "/settings"];
  const showKeysOverlay =
    !!server && !keysLoading && hasKeys === false && pathname !== "/" &&
    !KEYS_EXEMPT_ROUTES.some((r) => pathname.startsWith(r));
  const [reportOpen, setReportOpen] = useState(false);
  const [commandOpen, setCommandOpen] = useState(false);
  const [guideOpen, setGuideOpen] = useState<boolean>(() => {
    try {
      return localStorage.getItem(GUIDE_OPEN_KEY) === "1";
    } catch {
      return false;
    }
  });
  const shell = useShellLayout();
  const { toggleCollapsed } = shell;
  const [isMobile, setIsMobile] = useState(() => window.matchMedia(MOBILE_MEDIA_QUERY).matches);

  useEffect(() => {
    const query = window.matchMedia(MOBILE_MEDIA_QUERY);
    const update = () => setIsMobile(query.matches);
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);

  const toggleGuide = () => {
    const next = !guideOpen;
    setGuideOpen(next);
    try {
      localStorage.setItem(GUIDE_OPEN_KEY, next ? "1" : "0");
    } catch {
      // Not remembered; the panel still opens.
    }
  };

  const isFullBleed =
    FULL_BLEED_ROUTES.includes(pathname) ||
    FULL_BLEED_PATTERNS.some((re) => re.test(pathname));

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setCommandOpen((value) => !value);
      }
      if ((event.metaKey || event.ctrlKey) && event.key === "\\") {
        event.preventDefault();
        toggleCollapsed();
      }
      if (event.key === "Escape") setCommandOpen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [toggleCollapsed]);

  usePrefetchData();
  const queryClient = useQueryClient();
  useViewFacts(() => routeFacts(pathname, search, queryClient));

  const showCurrency = pathname !== "/" && (!isMobile || ["/portfolio", "/trade", "/bots"].includes(pathname));
  const showNotifications = !isMobile || ["/", "/bots", "/routines"].includes(pathname);

  return (
    <div
      className={`app-shell ${shell.collapsed ? "is-collapsed" : ""} ${shell.focusMode ? "is-focus-mode" : ""} ${shell.resizing ? "is-resizing" : ""} ${guideOpen ? "has-guide" : ""}`}
      style={shell.style}
    >
      <a className="skip-link" href="#main-content">Skip to content</a>

      <header className="app-topbar">
        <div className="topbar-leading">
          <NavLink to="/" className="mobile-brand" aria-label="Griffin home">
            <img src="/griffin-logo.png" alt="" className="home-logo" width={34} height={34} />
          </NavLink>
          <button className="command-trigger" onClick={() => setCommandOpen(true)} aria-label="Open command menu">
            <Search />
            <span>Go anywhere</span>
            <kbd>⌘ K</kbd>
          </button>
        </div>
        <div className="topbar-actions">
          <ServerSelector />
          {showCurrency && <div className="desktop-control"><CurrencySelector /></div>}
          {showNotifications && <div className="desktop-control"><NotificationBell /></div>}
          <span className="toolbar-divider desktop-control" />
          <button
            onClick={toggleGuide}
            className={`icon-button ${guideOpen ? "is-active" : ""}`}
            title={guideOpen ? "Close the Get Started guide" : "Open the Get Started guide"}
            aria-label="Get started guide"
            aria-expanded={guideOpen}
            aria-controls="guide-panel"
          >
            <BookOpen />
          </button>
          <button
            onClick={() => setReportOpen(true)}
            className="icon-button desktop-control"
            title="Report an issue"
            aria-label="Report an issue"
          >
            <Bug />
          </button>
          <button
            onClick={toggleTheme}
            className="icon-button"
            title={`Theme: ${theme}. Click to change`}
            aria-label="Toggle theme"
          >
            {theme === "light" ? <Moon /> : theme === "dark" ? <Sun /> : <Eye />}
          </button>
        </div>
      </header>

      <div className="app-body">
        <aside className="app-sidebar" aria-label="Main navigation">
          <NavLink to="/" className="sidebar-brand" aria-label="Griffin home">
            <span className="brand-mark"><img src="/griffin-logo.png" alt="" className="home-logo" width={34} height={34} /></span>
            <span className="brand-copy"><strong>Griffin</strong><small>Trading workspace</small></span>
          </NavLink>

          <button className="sidebar-command" onClick={() => setCommandOpen(true)} aria-label="Search and navigate">
            <Search /><span>Search</span><kbd>⌘K</kbd>
          </button>

          <div className="sidebar-section-label"><span>Workspace</span></div>
          <nav className="sidebar-nav">
            {NAV_ITEMS.map(({ to, icon: Icon, label }) => (
              <NavLink
                key={to}
                to={to}
                end={to === "/"}
                className={({ isActive }) => `sidebar-link ${isActive ? "is-active" : ""}`}
                title={label}
                aria-label={label}
              >
                <Icon className="sidebar-link-icon" />
                <span className="sidebar-link-label">{label}</span>
                <span className="sidebar-active-dot" />
              </NavLink>
            ))}
          </nav>

          <div className="sidebar-bottom">
            <div className="sidebar-status">
              <span className="status-orb" />
              <span><strong>Workspace ready</strong><small>Your trading command center</small></span>
            </div>
            <button className="sidebar-collapse" onClick={shell.toggleCollapsed} aria-label={shell.collapsed ? "Expand sidebar" : "Collapse sidebar"} title="Toggle sidebar · ⌘\">
              <PanelLeft /><span>Collapse</span><kbd>⌘\</kbd>
            </button>
          </div>
        </aside>

        <div
          className="sidebar-resizer"
          role="separator"
          aria-label="Resize navigation"
          aria-orientation="vertical"
          aria-valuemin={200}
          aria-valuemax={288}
          aria-valuenow={shell.railWidth}
          tabIndex={0}
          onPointerDown={shell.onResizeStart}
          onKeyDown={shell.onResizeKeyDown}
          onDoubleClick={shell.toggleCollapsed}
        ><span /></div>

        <main id="main-content" className={`app-main ${isFullBleed ? "is-full-bleed" : ""}`} tabIndex={-1}>
          <RelaunchBanner />
          <TelemetryConsentBanner />
          <ErrorBoundary resetKey={`${pathname}${search}`}>
            <div key={pathname} className="route-transition">
              <Outlet key={server} />
            </div>
          </ErrorBoundary>
        </main>

        {guideOpen && <GuidePanel onClose={toggleGuide} />}
      </div>

      <nav className="mobile-dock" aria-label="Mobile navigation">
        {MOBILE_NAV.map(({ to, icon: Icon, label }) => (
          <NavLink key={to} to={to} end={to === "/"} className={({ isActive }) => `mobile-dock-link ${isActive ? "is-active" : ""}`}>
            <Icon /><span>{label}</span>
          </NavLink>
        ))}
        <button className="mobile-dock-link" onClick={() => setCommandOpen(true)}>
          <Search /><span>More</span>
        </button>
      </nav>

      <button className="focus-mode-toggle icon-button" onClick={shell.toggleFocusMode} aria-label={shell.focusMode ? "Exit focus mode" : "Enter focus mode"} title={shell.focusMode ? "Show workspace chrome" : "Focus mode"}>
        {shell.focusMode ? <Minimize2 /> : <Maximize2 />}
      </button>

      <ChatBubble />
      {showKeysOverlay && <ConnectKeysOverlay />}
      <ReportIssueDialog open={reportOpen} onClose={() => setReportOpen(false)} />
      <CommandPalette
        open={commandOpen}
        onClose={() => setCommandOpen(false)}
        onNavigate={navigate}
        onOpenGuide={() => { if (!guideOpen) toggleGuide(); }}
        onReportIssue={() => setReportOpen(true)}
      />
    </div>
  );
}
