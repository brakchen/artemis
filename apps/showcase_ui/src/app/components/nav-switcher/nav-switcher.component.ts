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

import { Component, ChangeDetectionStrategy, inject } from '@angular/core';

import { RouterLink, RouterLinkActive } from '@angular/router';

import { I18nService } from '../../core/i18n/i18n.service';
import { TranslatePipe } from '../../core/i18n/translate.pipe';

@Component({
  selector: 'app-nav-switcher',
  standalone: true,
  imports: [RouterLink, RouterLinkActive, TranslatePipe],
  template: `
    <nav class="floating-nav-switcher" aria-label="Main Navigation">
      <a 
        routerLink="/" 
        routerLinkActive="active" 
        [routerLinkActiveOptions]="{exact: true}"
        class="nav-tab-btn"
        title="Return to Home Launcher to start a new task"
      >
        <span class="material-symbols-outlined tab-icon">add_task</span>
        <span class="tab-label">{{ 'New / Home' | t }}</span>
      </a>
      <a 
        routerLink="/workspace" 
        routerLinkActive="active" 
        class="nav-tab-btn"
        title="Open Workspace"
      >
        <span class="material-symbols-outlined tab-icon">space_dashboard</span>
        <span class="tab-label">{{ 'Workspace' | t }}</span>
      </a>
      <button
        type="button"
        class="lang-toggle-btn"
        (click)="i18n.toggle()"
        [attr.aria-label]="i18n.isZh() ? 'Switch to English' : '切换为中文'"
        [title]="i18n.isZh() ? 'Switch to English' : '切换为中文'"
      >
        <span class="material-symbols-outlined tab-icon">language</span>
        <span class="tab-label">{{ i18n.isZh() ? 'EN' : '中文' }}</span>
      </button>
    </nav>
  `,
  changeDetection: ChangeDetectionStrategy.Eager,
  styleUrls: ['./nav-switcher.component.scss']
})
export class NavSwitcherComponent {
  public readonly i18n = inject(I18nService);
}
