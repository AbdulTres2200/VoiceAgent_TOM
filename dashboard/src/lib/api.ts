// Debug: log env var at module load
console.log('[API] NEXT_PUBLIC_API_URL from env:', process.env.NEXT_PUBLIC_API_URL)

// Use runtime detection for API URL
function getApiBase(): string {
  const envUrl = process.env.NEXT_PUBLIC_API_URL

  // Check if we're in the browser
  if (typeof window !== 'undefined') {
    console.log('[API] Running in browser, hostname:', window.location.hostname)
    console.log('[API] NEXT_PUBLIC_API_URL:', envUrl)

    // Production: use the live backend
    if (window.location.hostname !== 'localhost') {
      const prodUrl = 'https://sarahvoiceagent-production.up.railway.app'
      console.log('[API] Using production URL:', prodUrl)
      return prodUrl
    }
  }

  // Development/SSR fallback
  const fallbackUrl = envUrl || 'http://localhost:8000'
  console.log('[API] Using fallback URL:', fallbackUrl)
  return fallbackUrl
}

export interface Technician {
  id: number
  name: string
  email: string | null
  phone: string | null
  status: string
  active: boolean
  zoneIds: number[]
  businessUnitId: number
  location: {
    latitude: number | null
    longitude: number | null
  }
  skills: {
    Skill_Well_Pump: string | null
    Skill_Water_Heaters: string | null
    Dispatchable: string | null
    Skill_Gas_Lines: string | null
    Skill_Sewers_Mainline: string | null
    Skill_Misc_Plumbing: string | null
    'Is Excavator': string | null
  }
}

export async function getTechnicians(): Promise<Technician[]> {
  const res = await fetch(`${getApiBase()}/api/technicians`, {
    cache: 'no-store'
  })
  const data = await res.json()
  return data.technicians
}

export async function updateTechnician(id: number, updates: {
  name?: string
  email?: string
  phone?: string
  business_unit_id?: number
  dispatchable?: string | null
  skill_sewers_mainline?: string | null
  skill_water_heaters?: string | null
  skill_misc_plumbing?: string | null
  skill_gas_lines?: string | null
  skill_well_pump?: string | null
  is_excavator?: string | null
}) {
  const res = await fetch(`${getApiBase()}/api/technicians/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(updates)
  })
  return res.json()
}

export interface Excavator extends Technician {
  // Excavator inherits email and phone from Technician
  // No additional fields needed
}

export async function getExcavators(): Promise<Excavator[]> {
  const res = await fetch(`${getApiBase()}/api/excavators`, {
    cache: 'no-store'
  })
  const data = await res.json()
  return data.excavators
}

export async function updateExcavatorEmail(id: number, email: string | null) {
  const res = await fetch(`${getApiBase()}/api/excavators/${id}/email`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email })
  })
  return res.json()
}
