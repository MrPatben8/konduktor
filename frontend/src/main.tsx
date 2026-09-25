import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import '@fontsource-variable/geist'
import '@fontsource-variable/geist-mono'
import './index.css'
import App from './App.tsx'
import { UpdateCheck } from './components/UpdateDialog.tsx'
import { AmbientBackdrop } from './components/AmbientBackdrop.tsx'
import { ConfirmHost } from './lib/confirm.tsx'
import { preventHistoryNavigation } from './lib/noNavigation.ts'

preventHistoryNavigation()

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      refetchOnWindowFocus: false,
    },
  },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      {/* Behind everything, the picker screen included: every surface is glass over it. */}
      <AmbientBackdrop />
      <App />
      {/* Mounted beside App so the update dialog shows even on the picker screen. */}
      <UpdateCheck />
      {/* askConfirm()'s dialogs — the app's replacement for window.confirm. */}
      <ConfirmHost />
    </QueryClientProvider>
  </StrictMode>,
)
