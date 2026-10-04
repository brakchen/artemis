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

import { TestBed } from '@angular/core/testing';

import { I18nService } from './i18n.service';

describe('I18nService', () => {
  let i18n: I18nService;

  beforeEach(() => {
    localStorage.clear();
    TestBed.configureTestingModule({});
    i18n = TestBed.inject(I18nService);
    i18n.setLang('zh');
  });

  afterEach(() => {
    localStorage.clear();
  });

  it('returns the source string unchanged in English', () => {
    i18n.setLang('en');
    expect(i18n.t('Run Task')).toBe('Run Task');
  });

  it('translates an exact key and falls back to English for unknown text', () => {
    expect(i18n.t('Run Task')).toBe('执行任务');
    expect(i18n.t('Some sentence nobody translated.')).toBe('Some sentence nobody translated.');
  });

  it('keeps empty input as-is', () => {
    expect(i18n.t('')).toBe('');
  });

  it('expands wildcard templates for dynamically composed labels', () => {
    expect(i18n.t('Tapping on "Send"')).toBe('点击 "Send"');
    expect(i18n.t('Waiting for 5 seconds...')).toBe('等待 5 秒…');
    expect(i18n.t('Python 3.12.7 Ready')).toBe('Python 3.12.7 已就绪');
    expect(i18n.t('3/5 segments saved')).toBe('3/5 段已保存');
  });

  it('prefers the more specific wildcard template over a generic one', () => {
    // 'Python * Ready' (longer literal) must win over any shorter pattern.
    expect(i18n.t('Python 3.12.7 Ready')).toBe('Python 3.12.7 已就绪');
  });

  it('translates the pieces captured by a wildcard template recursively', () => {
    // 'Steps 3–5' captured by '* condensed into a short memory*' itself hits 'Steps *–*'.
    expect(i18n.t('Steps 3–5 condensed into a short memory')).toBe('第 3–5 步已压缩为短期记忆');
  });

  it('keeps the checker label source distinct from the USB-guide "Check"', () => {
    // 'Check' 是 USB 授权指引里的动词（勾选），校验块标题用 'Checks'。
    expect(i18n.t('Check')).toBe('勾选');
    expect(i18n.t('Checks')).toBe('检查');
  });

  it('switches <html lang> with the language', () => {
    i18n.setLang('en');
    expect(document.documentElement.lang).toBe('en');
    i18n.setLang('zh');
    expect(document.documentElement.lang).toBe('zh-CN');
  });

  it('remembers the choice across service instances', () => {
    i18n.setLang('en');
    expect(TestBed.inject(I18nService).lang()).toBe('en');
  });
});
