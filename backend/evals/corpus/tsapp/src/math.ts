export interface Range {
  min: number;
  max: number;
}

export type Clamp = (value: number, range: Range) => number;

export function clamp(value: number, range: Range): number {
  return Math.min(range.max, Math.max(range.min, value));
}

export const double = (value: number): number => value * 2;

export function sum(values: number[]): number {
  return values.reduce((total, v) => total + v, 0);
}

export class Counter {
  private count = 0;

  increment(step: number = 1): number {
    this.count += step;
    return this.count;
  }

  reset(): void {
    this.count = 0;
  }
}
