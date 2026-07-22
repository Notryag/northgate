import { lazy, Suspense, useEffect, useMemo, useState } from "react";
import { Button, Layout, Menu, Tooltip } from "antd";
import {
  BarChart3,
  Gauge,
  KeyRound,
  Menu as MenuIcon,
  ReceiptText,
  SearchCode,
} from "lucide-react";
import { Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "./auth";
import { AccessDialog } from "./components/AccessDialog";

const OverviewPage = lazy(() => import("./pages/OverviewPage").then((module) => ({ default: module.OverviewPage })));
const RequestsPage = lazy(() => import("./pages/RequestsPage").then((module) => ({ default: module.RequestsPage })));
const RequestDetailPage = lazy(() => import("./pages/RequestDetailPage").then((module) => ({ default: module.RequestDetailPage })));
const UsagePage = lazy(() => import("./pages/UsagePage").then((module) => ({ default: module.UsagePage })));
const PricingPage = lazy(() => import("./pages/PricingPage").then((module) => ({ default: module.PricingPage })));

const { Header, Sider, Content } = Layout;

const navigation = [
  { key: "/overview", label: "Overview", icon: <Gauge size={17} /> },
  { key: "/requests", label: "Requests", icon: <SearchCode size={17} /> },
  { key: "/usage", label: "Usage", icon: <BarChart3 size={17} /> },
  { key: "/pricing", label: "Pricing", icon: <ReceiptText size={17} /> },
];

export function App() {
  const location = useLocation();
  const navigate = useNavigate();
  const { operatorKey, openAccess } = useAuth();
  const [mobileOpen, setMobileOpen] = useState(false);

  useEffect(() => {
    const open = () => openAccess();
    window.addEventListener("northgate:unauthorized", open);
    return () => window.removeEventListener("northgate:unauthorized", open);
  }, [openAccess]);

  const selected = useMemo(
    () => navigation.find((item) => location.pathname.startsWith(item.key))?.key ?? "/overview",
    [location.pathname],
  );

  return (
    <Layout className="app-layout">
      <Sider
        className="app-sider"
        width={228}
        breakpoint="lg"
        collapsedWidth={0}
        collapsed={!mobileOpen && window.innerWidth < 992}
        onBreakpoint={(broken) => { if (!broken) setMobileOpen(false); }}
        trigger={null}
      >
        <div className="side-brand"><span className="brand-mark">N</span><div><strong>Northgate</strong><span>Operator Console</span></div></div>
        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={[selected]}
          items={navigation}
          onClick={({ key }) => { navigate(key); setMobileOpen(false); }}
        />
        <div className="side-foot"><span className={operatorKey ? "session-dot active" : "session-dot"} />{operatorKey ? "Session active" : "Access required"}</div>
      </Sider>
      <Layout>
        <Header className="app-header">
          <Button className="mobile-menu" type="text" icon={<MenuIcon size={19} />} onClick={() => setMobileOpen((value) => !value)} aria-label="Toggle navigation" />
          <span className="header-context">Operations</span>
          <Tooltip title="Change operator key">
            <Button icon={<KeyRound size={15} />} onClick={openAccess}>Access</Button>
          </Tooltip>
        </Header>
        <Content className="app-content">
          <Suspense fallback={<div className="route-loading">Loading</div>}>
            <Routes>
              <Route path="/overview" element={<OverviewPage />} />
              <Route path="/requests" element={<RequestsPage />} />
              <Route path="/requests/:requestId" element={<RequestDetailPage />} />
              <Route path="/usage" element={<UsagePage />} />
              <Route path="/pricing" element={<PricingPage />} />
              <Route path="*" element={<Navigate to="/overview" replace />} />
            </Routes>
          </Suspense>
        </Content>
      </Layout>
      <AccessDialog />
    </Layout>
  );
}
