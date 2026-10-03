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

/**
 * 运行时界面语言切换。
 *
 * 英文是源语言（模板里的原文），所以 en 不需要词典；zh 通过 `zh.ts` 查表，
 * 未收录的文案原样回退到英文。语言选择存 localStorage，并在无记录时跟随浏览器语言。
 */
@Injectable({ providedIn: 'root' })
export class I18nService {
  /** 点查用的类型视图：`zh` 用 satisfies 保留字面量键类型，这里放宽为可任意字符串索引。 */
  private readonly dict: Record<string, string> = zh;

  public readonly lang = signal<Lang>(this.readInitialLang());
  public readonly isZh = computed(() => this.lang() === 'zh');

  /** 翻译一条源文案；读取 lang() 信号以驱动界面重渲染。 */
  public t(source: string): string {
    if (!source) return source;
    if (this.lang() !== 'zh') return source;
    return this.dict[source] ?? source;
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
