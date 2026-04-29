const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

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
  const res = await fetch(`${API_BASE}/api/technicians`, {
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
  const res = await fetch(`${API_BASE}/api/technicians/${id}`, {
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
  const res = await fetch(`${API_BASE}/api/excavators`, {
    cache: 'no-store'
  })
  const data = await res.json()
  return data.excavators
}

export async function updateExcavatorEmail(id: number, email: string | null) {
  const res = await fetch(`${API_BASE}/api/excavators/${id}/email`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email })
  })
  return res.json()
}
