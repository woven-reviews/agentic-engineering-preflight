import { useEffect, useState } from "react"

const API = import.meta.env.VITE_API_URL ?? ""

type Status = "checking" | "ok" | "error"

export function App() {
  const [status, setStatus] = useState<Status>("checking")
  const [detail, setDetail] = useState("")

  useEffect(() => {
    fetch(`${API}/api/v1/utils/db-check/`)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        return r.json()
      })
      .then((d) => {
        setStatus("ok")
        setDetail(`database: ${d.database}`)
      })
      .catch((e) => {
        setStatus("error")
        setDetail(String(e))
      })
  }, [])

  const color = status === "ok" ? "#16a34a" : status === "error" ? "#dc2626" : "#666"

  return (
    <main style={{ fontFamily: "system-ui, sans-serif", padding: "3rem", maxWidth: 640 }}>
      <h1>Environment Preflight</h1>
      <p>
        If you can read this and see a green check below, your machine can build and run the
        full stack — frontend, backend, and database all came up and can talk to each other.
      </p>
      <p style={{ fontSize: "1.5rem", color }}>
        {status === "ok" ? "✅ Stack is up" : status === "error" ? "❌ Stack check failed" : "⏳ Checking…"}
      </p>
      <pre style={{ color }}>{detail}</pre>
    </main>
  )
}
