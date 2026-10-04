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

import { Pipe, PipeTransform, inject } from '@angular/core';

import { I18nService } from './i18n.service';

/**
 * 界面文案翻译：`{{ 'Some text' | t }}`。
 *
 * 刻意做成非纯 pipe：纯 pipe 只按入参缓存，切换语言后不会重新求值；
 * 非纯 pipe 每次变更检测都会重查，切语言立即生效。查找本身是字典命中，开销可忽略。
 */
@Pipe({
  name: 't',
  pure: false,
  standalone: true,
})
export class TranslatePipe implements PipeTransform {
  private readonly i18n = inject(I18nService);

  public transform(value: string | null | undefined): string {
    return this.i18n.t(value ?? '');
  }
}
