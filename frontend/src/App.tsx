import { Navigate, Outlet, Route, Routes, useParams } from 'react-router'
import { AppShell } from './components/shell/AppShell'
import { ErrorNotice } from './components/shell/Notice'
import { Button } from './components/shell/Controls'
import { Inbox } from './routes/Inbox'
import { Instances } from './routes/Instances'
import { Landing } from './routes/Landing'
import { Docs } from './routes/Docs'
import { NewProcess } from './routes/NewProcess'
import { Process } from './routes/Process'
import { ProcessChat } from './routes/ProcessChat'
import { Processes } from './routes/Processes'
import { Queue } from './routes/Queue'
import { Rule } from './routes/Rule'
import { Settings } from './routes/Settings'
import { Definition } from './routes/Definition'
import { MailNotifications } from './components/process/MailNotifications'
import { Metrics } from './routes/Metrics'
import { ProcessSettings } from './routes/ProcessSettings'
import { paths } from './lib/paths'
import { useSession } from './state/session'

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Landing />} />
      <Route path="/docs" element={<Docs />} />
      <Route path="/docs/:slug" element={<Docs />} />
      <Route path="/login" element={<Navigate to={paths.processes} replace />} />
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
  )
}

function Console() {
  const { user, identityError, restoreDefaultIdentity } = useSession()
  return (
    <AppShell>
      <div className="relative flex min-h-0 flex-1 flex-col">
        <MailNotifications />
        {user ? <Outlet /> : identityError ? (
          <ErrorNotice error={identityError} action={
            <Button onClick={restoreDefaultIdentity}>Usar identidad predeterminada</Button>
          } />
        ) : null}
      </div>
    </AppShell>
  )
}

function Legacy({ to }: { to: (id: number | string) => string }) {
  const processId = useParams().processId
  return <Navigate to={to(processId ?? '')} replace />
}
