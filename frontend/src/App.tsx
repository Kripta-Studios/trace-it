import { lazy, Suspense } from 'react'
import { Navigate, Outlet, Route, Routes, useParams, useLocation } from 'react-router'
import { AppShell } from './components/shell/AppShell'
import { Login } from './routes/Login'
import { MailNotifications } from './components/process/MailNotifications'
import { paths } from './lib/paths'
import { useSession } from './state/session'
const Inbox = lazy(() => import('./routes/Inbox').then(m => ({ default: m.Inbox })))
const Instances = lazy(() => import('./routes/Instances').then(m => ({ default: m.Instances })))
const Landing = lazy(() => import('./routes/Landing').then(m => ({ default: m.Landing })))
const Knowledge = lazy(() => import('./routes/Knowledge').then(m => ({ default: m.Knowledge })))
const Docs = lazy(() => import('./routes/Docs').then(m => ({ default: m.Docs })))
const NewProcess = lazy(() => import('./routes/NewProcess').then(m => ({ default: m.NewProcess })))
const Process = lazy(() => import('./routes/Process').then(m => ({ default: m.Process })))
const ProcessChat = lazy(() => import('./routes/ProcessChat').then(m => ({ default: m.ProcessChat })))
const Processes = lazy(() => import('./routes/Processes').then(m => ({ default: m.Processes })))
const Queue = lazy(() => import('./routes/Queue').then(m => ({ default: m.Queue })))
const Rule = lazy(() => import('./routes/Rule').then(m => ({ default: m.Rule })))
const Settings = lazy(() => import('./routes/Settings').then(m => ({ default: m.Settings })))
const Definition = lazy(() => import('./routes/Definition').then(m => ({ default: m.Definition })))
const Metrics = lazy(() => import('./routes/Metrics').then(m => ({ default: m.Metrics })))
const ProcessSettings = lazy(() => import('./routes/ProcessSettings').then(m => ({ default: m.ProcessSettings })))

export default function App() {
  return (
    <Suspense fallback={<div role="status" className="p-8 text-sm text-muted">Cargando…</div>}>
    <Routes>
      <Route path="/" element={<Home />} />
      <Route path="/docs" element={<Docs />} />
      <Route path="/docs/:slug" element={<Docs />} />
      <Route path="/login" element={<Login />} />
      <Route element={<Console />}>
        <Route path="/processes" element={<Processes />} />
        <Route path="/processes/new" element={<NewProcess />} />
        <Route path="/processes/:processId" element={<Inbox />} />
        <Route path="/processes/:processId/panel" element={<Process />} />
        <Route path="/processes/:processId/chat" element={<ProcessChat />} />
        <Route path="/processes/:processId/definition" element={<Legacy to={paths.processChat} />} />
        <Route path="/processes/:processId/definition/*" element={<Definition />} />
        <Route path="/processes/:processId/review" element={<Queue />} />
        <Route path="/processes/:processId/reception" element={<Legacy to={paths.processSettings} />} />
        <Route path="/processes/:processId/documentation" element={<Knowledge />} />
        <Route path="/processes/:processId/metrics" element={<Metrics />} />
        <Route path="/processes/:processId/settings" element={<ProcessSettings />} />
        <Route path="/processes/:processId/instances" element={<Instances />} />
        <Route path="/processes/:processId/rules/:ruleId" element={<Rule />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="/processes/:processId/rules" element={<Legacy to={paths.definitionManual} />} />
        <Route path="/processes/:processId/knowledge" element={<Legacy to={paths.definitionSources} />} />
        <Route path="/processes/:processId/sources" element={<Legacy to={paths.definitionSources} />} />
        <Route path="/processes/:processId/queue" element={<Legacy to={paths.review} />} />
        <Route path="/processes/:processId/audit" element={<Legacy to={paths.definitionManual} />} />
        <Route path="/processes/:processId/versions" element={<Legacy to={paths.definitionManual} />} />
      </Route>
      <Route path="*" element={<Navigate to={paths.processes} replace />} />
    </Routes>
    </Suspense>
  )
}

function Console() {
  const { user, identityError, mode, loading } = useSession()
  const location = useLocation()
  if (loading) return <Login />
  if (!user && mode === 'password' && !identityError) return <Navigate to={`/login?next=${encodeURIComponent(location.pathname + location.search)}`} replace />
  if (!user) return <Login />
  return (
    <AppShell>
      <div className="relative flex min-h-0 flex-1 flex-col">
        <MailNotifications />
        <Outlet />
      </div>
    </AppShell>
  )
}

function Legacy({ to }: { to: (id: number | string) => string }) {
  const processId = useParams().processId
  return <Navigate to={to(processId ?? '')} replace />
}

function Home() {
  const { mode, loading } = useSession()
  return loading || mode !== 'identity' ? <Login /> : <Landing />
}
