// filepath: src/types/index.ts
export interface SkillRatings {
  sewersMainline: number;
  waterHeaters: number;
  miscPlumbing: number;
  gasLines: number;
  wellPump: number;
}

export interface Technician {
  id: string;
  name: string;
  phone: string;
  zones: string[];
  team: string;
  dispatchable: boolean;
  skills: SkillRatings;
  isActive: boolean;
}

export interface Excavator {
  id: string;
  name: string;
  phone: string;
  zone: string;
  team: string;
  active: boolean;
}