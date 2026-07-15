import { BrowserRouter, Routes, Route } from "react-router"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"

import Layout from "@/components/layout"
import MeetingsPage from "@/pages/MeetingsPage"
import NewMeetingPage from "@/pages/NewMeetingPage"
import JobPage from "@/pages/JobPage"
import MeetingDetailPage from "@/pages/MeetingDetailPage"
import SpeakersPage from "@/pages/SpeakersPage"
import SettingsPage from "@/pages/SettingsPage"
import TasksPage from "@/pages/TasksPage"
import ProjectsPage from "@/pages/ProjectsPage"
import ProjectDetailPage from "@/pages/ProjectDetailPage"

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 5_000,
      retry: 1,
    },
  },
})

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route element={<Layout />}>
            <Route index element={<MeetingsPage />} />
            <Route path="new" element={<NewMeetingPage />} />
            <Route path="jobs/:id" element={<JobPage />} />
            <Route path="meetings/:id" element={<MeetingDetailPage />} />
            <Route path="speakers" element={<SpeakersPage />} />
            <Route path="settings" element={<SettingsPage />} />
            <Route path="tasks" element={<TasksPage />} />
            <Route path="projects" element={<ProjectsPage />} />
            <Route path="projects/:id" element={<ProjectDetailPage />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  )
}
