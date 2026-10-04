/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

import { Injectable, signal, computed } from '@angular/core';

import { zh } from './zh';

export type Lang = 'en' | 'zh';

const STORAGE_KEY = 'artemis.lang';

/** 正则字面量转义：通配模板里只有 `*` 是元字符，其余字符按字面匹配。 */
function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/**
 * 运行时界面语言切换。
 *
 * 英文是源语言（模板里的原文），所以 en 不需要词典；zh 通过 `zh.ts` 查表，
 * 未收录的文案原样回退到英文。语言选择存 localStorage，并在无记录时跟随浏览器语言。
 *
 * 词典里含 `*` 的键是通配模板，用于动态拼接的文案（如 `Tapping on "Send"`）：
 * `*` 匹配任意片段，按序替换到译文的 `*` 上。先精确命中，再按“字面量最长”的模板
 * 命中，避免泛化模板抢了更具体的那条。
 */
@Injectable({ providedIn: 'root' })
export class I18nService {
  /** 点查用的类型视图：`zh` 用 satisfies 保留字面量键类型，这里放宽为可任意字符串索引。 */
  private readonly dict: Record<string, string> = zh;

  /** 通配模板的预编译视图；惰性构建一次，后续点查只走数组遍历。 */
  private patterns?: { re: RegExp; tpl: string }[];

  /** 通配命中的结果缓存：词典是静态的，同一条动态文案每次变更检测都要重查。 */
  private readonly patternCache = new Map<string, string>();

  public readonly lang = signal<Lang>(this.readInitialLang());
  public readonly isZh = computed(() => this.lang() === 'zh');

  /** 翻译一条源文案；读取 lang() 信号以驱动界面重渲染。 */
  public t(source: string): string {
    return this.resolve(source, 0);
  }

  /**
   * 精确查表 → 通配模板；depth 用于把模板捕获到的片段再翻一层，
   * 这样 `Agent Architecture: *` 这种两段拼接的文案也能整句翻过来。
   */
  private resolve(source: string, depth: number): string {
    if (!source) return source;
    if (this.lang() !== 'zh') return source;
    const direct = this.dict[source];
    if (direct !== undefined) return direct;
    if (depth >= 3) return source;
    return this.expandPattern(source, depth) ?? source;
  }

  /** 精确查不到时按通配模板匹配；不命中返回 null。 */
  private expandPattern(source: string, depth: number): string | null {
    if (depth === 0) {
      const cached = this.patternCache.get(source);
      if (cached !== undefined) return cached;
    }
    let hit: string | null = null;
    for (const { re, tpl } of this.getPatterns()) {
      const m = re.exec(source);
      if (!m) continue;
      let group = 1;
      // 片段再翻一层：`Agent Architecture: *` 能把后半段也翻掉。
      hit = tpl.replace(/\*/g, () => this.resolve(m[group++] ?? '', depth + 1));
      break;
    }
    if (depth === 0) {
      // 上限只防后端动态文案把缓存撑大；到顶整体清空即可。
      if (this.patternCache.size > 2000) this.patternCache.clear();
      if (hit !== null) this.patternCache.set(source, hit);
    }
    return hit;
  }

  /** 字面量越长越具体，排在前面：泛化模板不会抢走更具体的那条。 */
  private getPatterns(): { re: RegExp; tpl: string }[] {
    if (!this.patterns) {
      this.patterns = Object.entries(this.dict)
        .filter(([key]) => key.includes('*'))
        .sort(([a], [b]) => b.replace(/\*/g, '').length - a.replace(/\*/g, '').length)
        .map(([key, tpl]) => ({
          re: new RegExp(`^${key.split('*').map(escapeRegExp).join('([\\s\\S]*?)')}$`),
          tpl
        }));
    }
    return this.patterns;
  }

  /** 在中/英文之间切换，并记住选择。 */
  public toggle(): void {
    this.setLang(this.lang() === 'zh' ? 'en' : 'zh');
  }

  constructor() {
    // 初始语言也要同步到 <html lang>：否则刷新后内容是中文、lang 属性却还是 en
    this.applyDocumentLang(this.lang());
  }

  public setLang(lang: Lang): void {
    this.lang.set(lang);
    try {
      localStorage.setItem(STORAGE_KEY, lang);
    } catch {
      // localStorage 可能被禁用，忽略即可（只是不记住选择）
    }
    this.applyDocumentLang(lang);
  }

  private applyDocumentLang(lang: Lang): void {
    document.documentElement.lang = lang === 'zh' ? 'zh-CN' : 'en';
  }

  private readInitialLang(): Lang {
    try {
      const saved = localStorage.getItem(STORAGE_KEY);
      if (saved === 'zh' || saved === 'en') {
        return saved;
      }
    } catch {
      // 忽略：读不到就按浏览器语言判断
    }
    return navigator.language?.toLowerCase().startsWith('zh') ? 'zh' : 'en';
  }
}
