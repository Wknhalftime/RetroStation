import { useState } from 'react'
import { FolderSearch } from 'lucide-react'
import { PageHeader } from '@/components/ui/PageHeader'
import { apiFetch } from '@/api/client'

// ---------------------------------------------------------------------------
// ScannerActions
// ---------------------------------------------------------------------------

export function ScannerActions() {
  const [scanPath, setScanPath] = useState('')
  const [status, setStatus] = useState<'idle' | 'loading' | 'success' | 'error'>('idle')
  const [message, setMessage] = useState<string | null>(null)

  async function handleScan() {
    const trimmed = scanPath.trim()
    if (!trimmed) return

    setStatus('loading')
    setMessage(null)

    try {
      await apiFetch('/api/v1/library/scan', {
        method: 'POST',
        body: JSON.stringify({ path: trimmed }),
      })
      setStatus('success')
      setMessage('Scan started successfully.')
    } catch (err) {
      setStatus('error')
      setMessage(err instanceof Error ? err.message : 'Scan failed.')
    }
  }

  return (
    <div>
      <PageHeader
        title="Scanner"
        description="Trigger a library scan to ingest new files."
      />

      <div className="max-w-lg rounded-lg border border-gray-200 bg-white p-6">
        <div className="mb-4 flex items-center gap-2 text-gray-600">
          <FolderSearch className="h-5 w-5 shrink-0" />
          <p className="text-sm font-medium">Scan Path</p>
        </div>

        <div className="flex gap-2">
          <input
            type="text"
            value={scanPath}
            onChange={(e) => setScanPath(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') handleScan()
            }}
            placeholder="/mnt/music or C:\Music"
            className="flex-1 rounded-md border border-gray-300 px-3 py-2 text-sm placeholder-gray-400 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
          />
          <button
            onClick={handleScan}
            disabled={status === 'loading' || !scanPath.trim()}
            className="inline-flex items-center gap-1.5 rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
          >
            {status === 'loading' ? 'Scanning…' : 'Scan'}
          </button>
        </div>

        {message && (
          <p
            className={`mt-3 text-sm ${
              status === 'success' ? 'text-green-600' : 'text-red-500'
            }`}
          >
            {message}
          </p>
        )}
      </div>
    </div>
  )
}
