// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import type { ReactNode } from 'react'
import { Navigate, Route, Routes, useLocation, Link } from 'react-router-dom'
import { LoginPage } from './pages/LoginPage'
import { ProjectsPage } from './pages/ProjectsPage'
import { ProjectDetailPage } from './pages/ProjectDetailPage'
import { VariantWorkspacePage } from './pages/VariantWorkspacePage'
import { SubmissionBatchDetailPage } from './pages/SubmissionBatchDetailPage'
import { SubmissionReviewPage } from './pages/SubmissionReviewPage'
import { ChangePasswordPage } from './pages/ChangePasswordPage'
import { AdminCreateUserPage } from './pages/AdminCreateUserPage'
import { AdminUsersPage } from './pages/AdminUsersPage'
import { ProfilePage } from './pages/ProfilePage'
import { AppearancePage } from './pages/AppearancePage'
import { Layout } from './components/Layout'
import { useAuth } from './auth/authStore'
import { useAuthBootstrap } from './auth/useAuthBootstrap'

/**
 * Guards a private route. Subscribes to auth via useAuth so that a token change
 * (e.g. a 401 clearing the session mid-session) re-renders and redirects to
 * /login, preserving where the user was trying to go.
 *
 * Also enforces the forced first-login password change: while
 * must_change_password is true, every protected route redirects to
 * /change-password and the rest of the app is unreachable.
 */
function ProtectedRoute({ children }: { children: ReactNode }) {
  const { token, user } = useAuth()
  const location = useLocation()
  if (!token) return <Navigate to="/login" replace state={{ from: location.pathname }} />
  if (user?.must_change_password) return <Navigate to="/change-password" replace />
  return <>{children}</>
}

/** Like ProtectedRoute, but additionally requires role=ADMIN. */
function AdminRoute({ children }: { children: ReactNode }) {
  const { token, user } = useAuth()
  const location = useLocation()
  if (!token) return <Navigate to="/login" replace state={{ from: location.pathname }} />
  if (user?.must_change_password) return <Navigate to="/change-password" replace />
  if (user?.role !== 'ADMIN') return <Navigate to="/projects" replace />
  return <>{children}</>
}

function NotFoundPage() {
  return (
    <Layout>
      <div className="card" style={{ textAlign: 'center' }}>
        <h1 className="mb-16">Page not found</h1>
        <p className="info-msg mb-16">The page you’re looking for doesn’t exist.</p>
        <Link to="/projects" className="btn btn-primary">← Back to projects</Link>
      </div>
    </Layout>
  )
}

export default function App() {
  // Wait for the user profile to hydrate on a hard refresh before routing, so
  // the forced-password-change gate is evaluated correctly.
  const ready = useAuthBootstrap()
  if (!ready) {
    return (
      <div className="login-wrap">
        <div className="card login-card">
          <p className="info-msg">Loading…</p>
        </div>
      </div>
    )
  }

  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/change-password" element={<ChangePasswordPage />} />

      {/* Projects, variants and scanning */}
      <Route path="/projects" element={<ProtectedRoute><ProjectsPage /></ProtectedRoute>} />
      {/* Bare project URL (existing links/bookmarks) lands on the default tab.
          The relative target keeps the :projectId segment. */}
      <Route path="/projects/:projectId" element={<ProtectedRoute><Navigate to="variants" replace /></ProtectedRoute>} />
      <Route path="/projects/:projectId/:tab" element={<ProtectedRoute><ProjectDetailPage /></ProtectedRoute>} />
      {/* One scan batch, reached from the Scanning tab. Ranked above the :tab
          route by the router's static-segments-win specificity. */}
      <Route
        path="/projects/:projectId/scanning/batches/:batchId"
        element={<ProtectedRoute><SubmissionBatchDetailPage /></ProtectedRoute>}
      />
      {/* One submission of that batch, opened from a queue row.
          Static segments outrank :tab here too. */}
      <Route
        path="/projects/:projectId/scanning/batches/:batchId/submissions/:submissionId"
        element={<ProtectedRoute><SubmissionReviewPage /></ProtectedRoute>}
      />
      <Route path="/variants/:variantId" element={<ProtectedRoute><VariantWorkspacePage /></ProtectedRoute>} />

      {/* Self-service profile */}
      <Route path="/profile" element={<ProtectedRoute><ProfilePage /></ProtectedRoute>} />

      {/* Self-service appearance / theme */}
      <Route path="/appearance" element={<ProtectedRoute><AppearancePage /></ProtectedRoute>} />

      {/* Admin */}
      <Route path="/admin/users" element={<AdminRoute><AdminUsersPage /></AdminRoute>} />
      <Route path="/admin/users/new" element={<AdminRoute><AdminCreateUserPage /></AdminRoute>} />

      <Route path="/" element={<Navigate to="/projects" replace />} />
      <Route path="*" element={<ProtectedRoute><NotFoundPage /></ProtectedRoute>} />
    </Routes>
  )
}
