'use client';

import { useState, useMemo, useEffect } from 'react';
import { Search, ChevronLeft, ChevronRight, Loader2, RefreshCw, Check } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  getTechnicians,
  updateTechnician,
  getExcavators,
  Technician as ApiTechnician,
  Excavator as ApiExcavator
} from '@/lib/api';

const ITEMS_PER_PAGE = 10;

type SkillKey = 'Skill_Sewers_Mainline' | 'Skill_Water_Heaters' | 'Skill_Misc_Plumbing' | 'Skill_Gas_Lines' | 'Skill_Well_Pump';

const skillApiKeyMap: Record<SkillKey, string> = {
  'Skill_Sewers_Mainline': 'skill_sewers_mainline',
  'Skill_Water_Heaters': 'skill_water_heaters',
  'Skill_Misc_Plumbing': 'skill_misc_plumbing',
  'Skill_Gas_Lines': 'skill_gas_lines',
  'Skill_Well_Pump': 'skill_well_pump',
};

type EditingField = {
  id: number;
  field: 'email' | 'phone' | SkillKey | 'dispatchable' | 'is_excavator';
  tab: 'tech' | 'exc';
};

export default function Dashboard() {
  // Technicians state
  const [technicians, setTechnicians] = useState<ApiTechnician[]>([]);
  const [techLoading, setTechLoading] = useState(true);
  const [techLastUpdated, setTechLastUpdated] = useState<Date | null>(null);

  // Excavators state
  const [excavators, setExcavators] = useState<ApiExcavator[]>([]);
  const [excLoading, setExcLoading] = useState(true);
  const [excLastUpdated, setExcLastUpdated] = useState<Date | null>(null);

  // Editing state
  const [editing, setEditing] = useState<EditingField | null>(null);
  const [editValue, setEditValue] = useState('');
  const [savingId, setSavingId] = useState<number | null>(null);
  const [savedId, setSavedId] = useState<number | null>(null);

  // Search and pagination
  const [searchTerm, setSearchTerm] = useState('');
  const [techPage, setTechPage] = useState(1);
  const [excPage, setExcPage] = useState(1);

  // Fetch technicians from API
  const fetchTechnicians = async () => {
    setTechLoading(true);
    try {
      const data = await getTechnicians();
      setTechnicians(data);
      setTechLastUpdated(new Date());
    } catch (error) {
      console.error('Failed to fetch technicians:', error);
    } finally {
      setTechLoading(false);
    }
  };

  // Fetch excavators from API
  const fetchExcavators = async () => {
    setExcLoading(true);
    try {
      const data = await getExcavators();
      setExcavators(data);
      setExcLastUpdated(new Date());
    } catch (error) {
      console.error('Failed to fetch excavators:', error);
    } finally {
      setExcLoading(false);
    }
  };

  useEffect(() => {
    fetchTechnicians();
    fetchExcavators();
  }, []);

  // Filtered and paginated technicians
  const filteredTechnicians = useMemo(() => {
    return technicians.filter((tech) =>
      tech.name.toLowerCase().includes(searchTerm.toLowerCase())
    );
  }, [technicians, searchTerm]);

  const paginatedTechnicians = useMemo(() => {
    const start = (techPage - 1) * ITEMS_PER_PAGE;
    return filteredTechnicians.slice(start, start + ITEMS_PER_PAGE);
  }, [filteredTechnicians, techPage]);

  const totalTechPages = Math.ceil(filteredTechnicians.length / ITEMS_PER_PAGE);

  // Filtered and paginated excavators
  const filteredExcavators = useMemo(() => {
    return excavators.filter((exc) =>
      exc.name.toLowerCase().includes(searchTerm.toLowerCase())
    );
  }, [excavators, searchTerm]);

  const paginatedExcavators = useMemo(() => {
    const start = (excPage - 1) * ITEMS_PER_PAGE;
    return filteredExcavators.slice(start, start + ITEMS_PER_PAGE);
  }, [filteredExcavators, excPage]);

  const totalExcPages = Math.ceil(filteredExcavators.length / ITEMS_PER_PAGE);

  // Reset page when search changes
  const handleSearchChange = (value: string) => {
    setSearchTerm(value);
    setTechPage(1);
    setExcPage(1);
  };

  // Start editing a field
  const startEditing = (id: number, field: EditingField['field'], currentValue: string | null, tab: 'tech' | 'exc') => {
    setEditing({ id, field, tab });
    setEditValue(currentValue || '');
  };

  // Cancel editing
  const cancelEditing = () => {
    setEditing(null);
    setEditValue('');
  };

  // Save field update
  const saveField = async (id: number, field: EditingField['field'], value: string | null, tab: 'tech' | 'exc') => {
    setSavingId(id);
    setEditing(null);

    try {
      let updatePayload: Record<string, string | null> = {};

      if (field === 'email') {
        updatePayload = { email: value };
      } else if (field === 'phone') {
        updatePayload = { phone: value };
      } else if (field === 'dispatchable') {
        updatePayload = { dispatchable: value };
      } else if (field === 'is_excavator') {
        updatePayload = { is_excavator: value };
      } else {
        // Skill field
        const apiKey = skillApiKeyMap[field as SkillKey];
        updatePayload = { [apiKey]: value };
      }

      const result = await updateTechnician(id, updatePayload);

      if (result.status === 'success' && result.technician) {
        // Update local state with returned data
        if (tab === 'tech') {
          setTechnicians(technicians.map(t =>
            t.id === id ? {
              ...t,
              email: result.technician.email ?? t.email,
              phone: result.technician.phone ?? t.phone,
              skills: result.technician.skills ?? t.skills
            } : t
          ));
        } else {
          setExcavators(excavators.map(e =>
            e.id === id ? {
              ...e,
              email: result.technician.email ?? e.email,
              phone: result.technician.phone ?? e.phone,
              skills: result.technician.skills ?? e.skills
            } : e
          ));
        }

        // Show saved indicator
        setSavedId(id);
        setTimeout(() => setSavedId(null), 1500);
      }
    } catch (error) {
      console.error('Failed to update:', error);
    } finally {
      setSavingId(null);
      setEditValue('');
    }
  };

  // Toggle handlers
  const handleToggle = async (id: number, field: 'dispatchable' | 'is_excavator', currentValue: string | null, tab: 'tech' | 'exc') => {
    const newValue = currentValue === 'YES' ? null : 'YES';
    await saveField(id, field, newValue, tab);
  };

  // Skill change handler
  const handleSkillChange = async (id: number, skill: SkillKey, value: string | null, tab: 'tech' | 'exc') => {
    await saveField(id, skill, value, tab);
  };

  const getStatusBadgeClass = (status: string) => {
    switch (status) {
      case 'Idle':
        return 'bg-green-100 text-green-800 border-green-200';
      case 'Working':
        return 'bg-blue-100 text-blue-800 border-blue-200';
      case 'Dispatched':
        return 'bg-orange-100 text-orange-800 border-orange-200';
      default:
        return 'bg-zinc-100 text-zinc-800 border-zinc-200';
    }
  };

  // Render editable text field (email/phone)
  const renderEditableText = (
    item: ApiTechnician | ApiExcavator,
    field: 'email' | 'phone',
    tab: 'tech' | 'exc'
  ) => {
    const value = item[field];
    const isEditing = editing?.id === item.id && editing?.field === field && editing?.tab === tab;
    const isSaving = savingId === item.id;
    const isSaved = savedId === item.id;

    if (isEditing) {
      return (
        <div className="flex items-center gap-1">
          <Input
            value={editValue}
            onChange={(e) => setEditValue(e.target.value)}
            placeholder={field === 'email' ? 'email@example.com' : '(555) 123-4567'}
            className="h-7 w-36 text-xs text-zinc-900 bg-white border-zinc-300"
            onKeyDown={(e) => {
              if (e.key === 'Enter') saveField(item.id, field, editValue.trim() || null, tab);
              if (e.key === 'Escape') cancelEditing();
            }}
            autoFocus
          />
          <Button size="sm" className="h-7 px-2" onClick={() => saveField(item.id, field, editValue.trim() || null, tab)}>
            Save
          </Button>
        </div>
      );
    }

    return (
      <button
        onClick={() => startEditing(item.id, field, value, tab)}
        disabled={isSaving}
        className="text-xs hover:underline cursor-pointer disabled:cursor-wait flex items-center gap-1"
      >
        {isSaving ? (
          <Loader2 className="h-3 w-3 animate-spin" />
        ) : isSaved ? (
          <Check className="h-3 w-3 text-green-600" />
        ) : null}
        {value ? (
          <span className="text-zinc-700">{value}</span>
        ) : (
          <span className="text-zinc-400 italic">Not set</span>
        )}
      </button>
    );
  };

  // Render toggle badge (Dispatchable/Is Excavator)
  const renderToggleBadge = (
    item: ApiTechnician | ApiExcavator,
    field: 'dispatchable' | 'is_excavator',
    tab: 'tech' | 'exc'
  ) => {
    const value = field === 'dispatchable'
      ? item.skills.Dispatchable
      : item.skills['Is Excavator'];
    const isSaving = savingId === item.id;
    const isSaved = savedId === item.id;

    return (
      <button
        onClick={() => handleToggle(item.id, field, value, tab)}
        disabled={isSaving}
        className="cursor-pointer disabled:cursor-wait flex items-center gap-1"
      >
        {isSaving ? (
          <Loader2 className="h-4 w-4 animate-spin" />
        ) : (
          <>
            {isSaved && <Check className="h-3 w-3 text-green-600" />}
            <Badge variant={value === 'YES' ? 'success' : 'secondary'}>
              {value === 'YES' ? 'YES' : 'NO'}
            </Badge>
          </>
        )}
      </button>
    );
  };

  // Render skill rating dots
  const renderSkillRating = (
    item: ApiTechnician | ApiExcavator,
    skill: SkillKey,
    tab: 'tech' | 'exc'
  ) => {
    const value = item.skills[skill];
    const rating = value ? parseInt(value) : 0;
    const isEditing = editing?.id === item.id && editing?.field === skill && editing?.tab === tab;
    const isSaving = savingId === item.id;
    const isSaved = savedId === item.id;

    if (isEditing) {
      return (
        <Select
          value={value || 'none'}
          onValueChange={(newValue) => handleSkillChange(item.id, skill, newValue === 'none' ? null : newValue, tab)}
        >
          <SelectTrigger className="w-16 h-7 text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="none">-</SelectItem>
            {[1, 2, 3, 4, 5].map((r) => (
              <SelectItem key={r} value={r.toString()}>{r}</SelectItem>
            ))}
          </SelectContent>
        </Select>
      );
    }

    return (
      <button
        className="flex items-center gap-1 hover:opacity-70 transition-opacity cursor-pointer disabled:cursor-wait"
        onClick={() => startEditing(item.id, skill, value, tab)}
        disabled={isSaving}
      >
        {isSaving ? (
          <Loader2 className="h-3 w-3 animate-spin" />
        ) : (
          <>
            {isSaved && <Check className="h-3 w-3 text-green-600 mr-1" />}
            {[1, 2, 3, 4, 5].map((star) => (
              <div
                key={star}
                className={`h-2.5 w-2.5 rounded-full border ${star <= rating ? 'bg-zinc-800 border-zinc-800' : 'bg-zinc-100 border-zinc-400'}`}
              />
            ))}
          </>
        )}
      </button>
    );
  };

  return (
    <div className="min-h-screen bg-zinc-50 p-6">
      <div className="mx-auto max-w-[1400px]">
        <div className="mb-6">
          <h1 className="text-2xl font-semibold text-zinc-900">Field Service Management</h1>
          <p className="text-zinc-500">Manage your technicians and excavation staff</p>
        </div>

        <Tabs defaultValue="technicians" className="w-full">
          <div className="flex items-center justify-between mb-4">
            <TabsList>
              <TabsTrigger value="technicians">Technicians</TabsTrigger>
              <TabsTrigger value="excavators">Excavators</TabsTrigger>
            </TabsList>

            <div className="flex items-center gap-2">
              <div className="relative">
                <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-zinc-400" />
                <Input
                  placeholder="Search by name..."
                  value={searchTerm}
                  onChange={(e) => handleSearchChange(e.target.value)}
                  className="pl-9 w-64"
                />
              </div>
            </div>
          </div>

          {/* Technicians Tab */}
          <TabsContent value="technicians" className="mt-0">
            <div className="rounded-lg border border-zinc-200 bg-white">
              <div className="p-4 border-b border-zinc-200 flex items-center justify-between">
                <div className="flex items-center gap-2 text-sm text-zinc-500">
                  {techLoading ? (
                    <span className="flex items-center gap-2">
                      <Loader2 className="h-4 w-4 animate-spin" />
                      Loading technicians...
                    </span>
                  ) : (
                    <span>
                      {technicians.length} technicians loaded from ServiceTitan
                      {techLastUpdated && (
                        <> &bull; Last updated: {techLastUpdated.toLocaleTimeString()}</>
                      )}
                    </span>
                  )}
                </div>
                <Button variant="outline" size="sm" onClick={fetchTechnicians} disabled={techLoading}>
                  <RefreshCw className={`h-4 w-4 mr-1 ${techLoading ? 'animate-spin' : ''}`} />
                  Refresh
                </Button>
              </div>
              {techLoading ? (
                <div className="p-12 flex items-center justify-center">
                  <Loader2 className="h-8 w-8 animate-spin text-zinc-400" />
                </div>
              ) : (
                <>
                  <div className="overflow-x-auto">
                    <table className="w-full">
                      <thead>
                        <tr className="border-b border-zinc-200 bg-zinc-50">
                          <th className="px-3 py-3 text-left text-xs font-medium text-zinc-600">Name</th>
                          <th className="px-3 py-3 text-left text-xs font-medium text-zinc-600">Email</th>
                          <th className="px-3 py-3 text-left text-xs font-medium text-zinc-600">Phone</th>
                          <th className="px-3 py-3 text-left text-xs font-medium text-zinc-600">Status</th>
                          <th className="px-3 py-3 text-left text-xs font-medium text-zinc-600">Dispatch</th>
                          <th className="px-3 py-3 text-left text-xs font-medium text-zinc-600">Excavator</th>
                          <th className="px-3 py-3 text-left text-xs font-medium text-zinc-600">Sewers</th>
                          <th className="px-3 py-3 text-left text-xs font-medium text-zinc-600">Water</th>
                          <th className="px-3 py-3 text-left text-xs font-medium text-zinc-600">Misc</th>
                          <th className="px-3 py-3 text-left text-xs font-medium text-zinc-600">Gas</th>
                          <th className="px-3 py-3 text-left text-xs font-medium text-zinc-600">Pump</th>
                        </tr>
                      </thead>
                      <tbody>
                        {paginatedTechnicians.map((tech) => (
                          <tr key={tech.id} className={`border-b border-zinc-200 hover:bg-zinc-50 ${!tech.active ? 'opacity-50' : ''}`}>
                            <td className="px-3 py-2 text-sm text-zinc-900 whitespace-nowrap">
                              {tech.name}
                            </td>
                            <td className="px-3 py-2">{renderEditableText(tech, 'email', 'tech')}</td>
                            <td className="px-3 py-2">{renderEditableText(tech, 'phone', 'tech')}</td>
                            <td className="px-3 py-2">
                              <Badge className={getStatusBadgeClass(tech.status)}>
                                {tech.status}
                              </Badge>
                            </td>
                            <td className="px-3 py-2">{renderToggleBadge(tech, 'dispatchable', 'tech')}</td>
                            <td className="px-3 py-2">{renderToggleBadge(tech, 'is_excavator', 'tech')}</td>
                            <td className="px-3 py-2">{renderSkillRating(tech, 'Skill_Sewers_Mainline', 'tech')}</td>
                            <td className="px-3 py-2">{renderSkillRating(tech, 'Skill_Water_Heaters', 'tech')}</td>
                            <td className="px-3 py-2">{renderSkillRating(tech, 'Skill_Misc_Plumbing', 'tech')}</td>
                            <td className="px-3 py-2">{renderSkillRating(tech, 'Skill_Gas_Lines', 'tech')}</td>
                            <td className="px-3 py-2">{renderSkillRating(tech, 'Skill_Well_Pump', 'tech')}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  {filteredTechnicians.length === 0 && (
                    <div className="p-8 text-center text-zinc-500">No technicians found</div>
                  )}
                  {totalTechPages > 1 && (
                    <div className="flex items-center justify-between p-4 border-t border-zinc-200">
                      <div className="text-sm text-zinc-500">
                        Showing {(techPage - 1) * ITEMS_PER_PAGE + 1} to{' '}
                        {Math.min(techPage * ITEMS_PER_PAGE, filteredTechnicians.length)} of{' '}
                        {filteredTechnicians.length} results
                      </div>
                      <div className="flex items-center gap-2">
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => setTechPage((p) => Math.max(1, p - 1))}
                          disabled={techPage === 1}
                        >
                          <ChevronLeft className="h-4 w-4" />
                        </Button>
                        <span className="text-sm text-zinc-600">
                          Page {techPage} of {totalTechPages}
                        </span>
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => setTechPage((p) => Math.min(totalTechPages, p + 1))}
                          disabled={techPage === totalTechPages}
                        >
                          <ChevronRight className="h-4 w-4" />
                        </Button>
                      </div>
                    </div>
                  )}
                </>
              )}
            </div>
          </TabsContent>

          {/* Excavators Tab */}
          <TabsContent value="excavators" className="mt-0">
            <div className="rounded-lg border border-zinc-200 bg-white">
              <div className="p-4 border-b border-zinc-200 flex items-center justify-between">
                <div className="flex items-center gap-2 text-sm text-zinc-500">
                  {excLoading ? (
                    <span className="flex items-center gap-2">
                      <Loader2 className="h-4 w-4 animate-spin" />
                      Loading excavators...
                    </span>
                  ) : (
                    <span>
                      {excavators.length} excavators loaded from ServiceTitan
                      {excLastUpdated && (
                        <> &bull; Last updated: {excLastUpdated.toLocaleTimeString()}</>
                      )}
                    </span>
                  )}
                </div>
                <Button variant="outline" size="sm" onClick={fetchExcavators} disabled={excLoading}>
                  <RefreshCw className={`h-4 w-4 mr-1 ${excLoading ? 'animate-spin' : ''}`} />
                  Refresh
                </Button>
              </div>
              {excLoading ? (
                <div className="p-12 flex items-center justify-center">
                  <Loader2 className="h-8 w-8 animate-spin text-zinc-400" />
                </div>
              ) : (
                <>
                  <div className="overflow-x-auto">
                    <table className="w-full">
                      <thead>
                        <tr className="border-b border-zinc-200 bg-zinc-50">
                          <th className="px-3 py-3 text-left text-xs font-medium text-zinc-600">Name</th>
                          <th className="px-3 py-3 text-left text-xs font-medium text-zinc-600">Email</th>
                          <th className="px-3 py-3 text-left text-xs font-medium text-zinc-600">Phone</th>
                          <th className="px-3 py-3 text-left text-xs font-medium text-zinc-600">Status</th>
                          <th className="px-3 py-3 text-left text-xs font-medium text-zinc-600">Dispatch</th>
                          <th className="px-3 py-3 text-left text-xs font-medium text-zinc-600">Sewers</th>
                          <th className="px-3 py-3 text-left text-xs font-medium text-zinc-600">Water</th>
                          <th className="px-3 py-3 text-left text-xs font-medium text-zinc-600">Misc</th>
                          <th className="px-3 py-3 text-left text-xs font-medium text-zinc-600">Gas</th>
                          <th className="px-3 py-3 text-left text-xs font-medium text-zinc-600">Pump</th>
                        </tr>
                      </thead>
                      <tbody>
                        {paginatedExcavators.map((exc) => (
                          <tr key={exc.id} className="border-b border-zinc-200 hover:bg-zinc-50">
                            <td className="px-3 py-2 text-sm text-zinc-900 whitespace-nowrap">{exc.name}</td>
                            <td className="px-3 py-2">{renderEditableText(exc, 'email', 'exc')}</td>
                            <td className="px-3 py-2">{renderEditableText(exc, 'phone', 'exc')}</td>
                            <td className="px-3 py-2">
                              <Badge className={getStatusBadgeClass(exc.status)}>
                                {exc.status}
                              </Badge>
                            </td>
                            <td className="px-3 py-2">{renderToggleBadge(exc, 'dispatchable', 'exc')}</td>
                            <td className="px-3 py-2">{renderSkillRating(exc, 'Skill_Sewers_Mainline', 'exc')}</td>
                            <td className="px-3 py-2">{renderSkillRating(exc, 'Skill_Water_Heaters', 'exc')}</td>
                            <td className="px-3 py-2">{renderSkillRating(exc, 'Skill_Misc_Plumbing', 'exc')}</td>
                            <td className="px-3 py-2">{renderSkillRating(exc, 'Skill_Gas_Lines', 'exc')}</td>
                            <td className="px-3 py-2">{renderSkillRating(exc, 'Skill_Well_Pump', 'exc')}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  {filteredExcavators.length === 0 && (
                    <div className="p-8 text-center text-zinc-500">No excavators found</div>
                  )}
                  {totalExcPages > 1 && (
                    <div className="flex items-center justify-between p-4 border-t border-zinc-200">
                      <div className="text-sm text-zinc-500">
                        Showing {(excPage - 1) * ITEMS_PER_PAGE + 1} to{' '}
                        {Math.min(excPage * ITEMS_PER_PAGE, filteredExcavators.length)} of{' '}
                        {filteredExcavators.length} results
                      </div>
                      <div className="flex items-center gap-2">
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => setExcPage((p) => Math.max(1, p - 1))}
                          disabled={excPage === 1}
                        >
                          <ChevronLeft className="h-4 w-4" />
                        </Button>
                        <span className="text-sm text-zinc-600">
                          Page {excPage} of {totalExcPages}
                        </span>
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => setExcPage((p) => Math.min(totalExcPages, p + 1))}
                          disabled={excPage === totalExcPages}
                        >
                          <ChevronRight className="h-4 w-4" />
                        </Button>
                      </div>
                    </div>
                  )}
                </>
              )}
            </div>
          </TabsContent>
        </Tabs>
      </div>
    </div>
  );
}
