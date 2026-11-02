import { provideHttpClient, withXhr } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ComponentFixture, TestBed, fakeAsync, tick, flush } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { of } from 'rxjs';

import { AgentService } from '../../services/agent.service';
import { SystemService, LlmProviderInfo, ModelConfigEnvResponse } from '../../services/system.service';
import { I18nService } from '../../core/i18n/i18n.service';
import { HomeComponent } from './home.component';

describe('Home provider configuration rendered form', () => {
  let fixture: ComponentFixture<HomeComponent>;
  let component: HomeComponent;
  let service: SystemService;
  let http: HttpTestingController;

  const secret = 'old-readiness-secret-must-never-refill';
  const provider: LlmProviderInfo = {
    name: 'my-mini', kind: 'anthropic', vendor: 'minimax',
    api_base: 'https://mini.invalid/anthropic', model: 'MiniMax-M3.1-Flash', fallback_model: 'MiniMax-M3',
    temperature: 0.2, timeout_seconds: 43, is_multimodal: true, enabled: true,
    key_configured: true, key_masked: '****1234'
  };
  const modelConfig: ModelConfigEnvResponse = {
    config_path: '/isolated/artemis.jsonc', config_filename: 'artemis.jsonc', config_content: '{}',
    default_model: { provider: 'google', model: 'legacy' }, presets: {},
    env_path: '/isolated/.env', env_filename: '.env', env_vars: []
  };

  function element<T extends HTMLElement = HTMLButtonElement>(id: string): T {
    return fixture.nativeElement.querySelector(`[data-testid="${id}"]`) as T;
  }

  function type(id: string, value: string): void {
    const input = element<HTMLInputElement>(id);
    input.value = value;
    input.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    tick();
  }

  function refresh(): void {
    http.expectOne('/api/system/llm-providers').flush({ providers: [provider], default_provider: 'my-mini', registry_path: '/isolated/registry.json' });
    http.expectOne('/api/system/model-config-env').flush(modelConfig);
    fixture.detectChanges();
  }

  beforeEach(async () => {
    TestBed.configureTestingModule({
      imports: [HomeComponent],
      providers: [provideHttpClient(withXhr()), provideHttpClientTesting(), provideRouter([]),
        { provide: AgentService, useValue: { getProTuningDefaults: () => of({ verification_level: 'final', explorer_mode: 'pro' }) } }]
    });
    service = TestBed.inject(SystemService);
    service.stopAutoPolling();
    http = TestBed.inject(HttpTestingController);
    TestBed.inject(I18nService).lang.set('en');
    fixture = TestBed.createComponent(HomeComponent);
    component = fixture.componentInstance;
    component.activeTab.set('diagnostics');
    component.modelSetupMode.set('custom');
    fixture.detectChanges();
    http.expectOne('/api/system/readiness').flush({ overall_ready: false, probes: [{
      id: 'gemini_api_key', status: 'fail', title: 'Credentials', metadata: { current_key: secret, api_keys: { google: secret, ocr: secret } }
    }], active_device: null, os_type: 'windows', timestamp: 1, blocker_count: 3, passed_blocker_count: 0 });
    refresh();
    http.expectOne('/api/system/emulator/status').flush({ status: 'idle' });
    http.expectOne('/api/system/adb/server').flush({ endpoint: { mode: 'local', host: '127.0.0.1', port: 5037 } });
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  });

  afterEach(() => {
    fixture.destroy();
    http.verify();
  });

  it('uses the MiniMax preset and manual IDs, preserves input on failure and retries without marking ready', fakeAsync(() => {
    element('minimax-preset').click();
    fixture.detectChanges();
    tick();
    expect(element<HTMLSelectElement>('provider-kind').value).toBe('anthropic');
    expect(element<HTMLSelectElement>('provider-vendor').value).toBe('minimax');
    expect(element<HTMLInputElement>('provider-base').value).toBe('https://api.minimax.cn/anthropic');
    type('provider-model', 'MiniMax-M3');
    type('provider-key', 'new-offline-key');
    element('provider-save').click();
    fixture.detectChanges();
    const failed = http.expectOne('/api/system/llm-providers');
    expect(failed.request.body).toEqual(jasmine.objectContaining({ kind: 'anthropic', vendor: 'minimax', model: 'MiniMax-M3', api_key: 'new-offline-key' }));
    expect(element<HTMLFieldSetElement>('provider-form').querySelector('fieldset')?.disabled).toBeTrue();
    expect(element<HTMLButtonElement>('provider-save').disabled).toBeTrue();
    failed.flush({ detail: 'Catalog denied. Check the endpoint.' }, { status: 400, statusText: 'Bad Request' });
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('Catalog denied');
    expect(element<HTMLInputElement>('provider-model').value).toBe('MiniMax-M3');
    expect(element<HTMLInputElement>('provider-key').value).toBe('new-offline-key');
    element('provider-save').click();
    http.expectOne('/api/system/llm-providers').flush({ status: 'success' });
    refresh();
    tick();
    fixture.detectChanges();
    expect(component.newProviderApiKey()).toBe('');
    expect(element<HTMLInputElement>('provider-key').value).toBe('');
    expect(element('migration-guidance').textContent).toContain('artemis init --provider minimax --name minimax');
    expect(service.isCredentialsReady()).toBeFalse();
    expect(service.isSkipCredentialsCheck()).toBeFalse();
    flush();
  }));

  it('edits public metadata with an always blank key, and sends the same draft to test, models and save', fakeAsync(() => {
    element('provider-edit').click();
    fixture.detectChanges();
    tick();
    expect(element<HTMLInputElement>('provider-key').value).toBe('');
    expect(element<HTMLInputElement>('provider-name').readOnly).toBeTrue();
    expect(element<HTMLInputElement>('provider-model').value).toBe('MiniMax-M3.1-Flash');
    element('provider-test').click();
    const test = http.expectOne('/api/system/llm-providers/test');
    expect(test.request.body).toEqual(jasmine.objectContaining({ name: 'my-mini', api_key: '', kind: 'anthropic', vendor: 'minimax', api_base: provider.api_base }));
    test.flush({ valid: true, message: 'Catalog authentication verified; capabilities untested.' });
    element('provider-models').click();
    const models = http.expectOne('/api/system/llm-providers/models');
    expect(models.request.body).toEqual(test.request.body);
    models.flush({ models: [{ id: 'MiniMax-M3' }], endpoint: 'https://mini.invalid/anthropic/v1/models' });
    element('provider-save').click();
    const save = http.expectOne('/api/system/llm-providers');
    expect(save.request.body).toEqual(jasmine.objectContaining(test.request.body));
    expect(save.request.body.temperature).toBeUndefined();
    expect(save.request.body.timeout_seconds).toBeUndefined();
    expect(save.request.body.enabled).toBeUndefined();
    save.flush({ status: 'success' });
    refresh();
    expect(element('migration-guidance').textContent).toContain('--name my-mini');
    flush();
  }));

  it('requires confirmation to clear and disable, while cancel editing sends no writes', fakeAsync(() => {
    element('provider-edit').click();
    fixture.detectChanges();
    tick();
    const confirm = spyOn(window, 'confirm').and.returnValue(false);
    element('provider-clear').click();
    http.expectNone('/api/system/llm-providers');
    expect(component.editingProvider()).toBe('my-mini');
    confirm.and.returnValue(true);
    element('provider-clear').click();
    const clear = http.expectOne('/api/system/llm-providers');
    expect(clear.request.body).toEqual({ name: 'my-mini', clear_api_key: true, confirm_clear: true, enabled: false, verify: false });
    clear.flush({ status: 'success' });
    refresh();
    element('provider-edit').click();
    fixture.detectChanges();
    tick();
    type('provider-key', 'discard-offline-key');
    element('provider-cancel').click();
    fixture.detectChanges();
    tick();
    expect(element<HTMLInputElement>('provider-key').value).toBe('');
    expect(component.editingProvider()).toBeNull();
    http.expectNone('/api/system/llm-providers');
    flush();
  }));

  it('changes draft protocol, keeps manual IDs after catalog failure, and never refills legacy keys', fakeAsync(() => {
    component.setModelSetupMode('custom');
    http.expectOne('/api/system/model-config-env').flush(modelConfig);
    expect(service.isSkipCredentialsCheck()).toBeFalse();
    element('provider-edit').click();
    fixture.detectChanges();
    tick();
    const select = element<HTMLSelectElement>('provider-kind');
    select.value = 'openai';
    select.dispatchEvent(new Event('change'));
    fixture.detectChanges();
    tick();
    type('provider-model', 'manual-model-unlisted');
    element('provider-models').click();
    const models = http.expectOne('/api/system/llm-providers/models');
    expect(models.request.body.kind).toBe('openai');
    expect(models.request.body.vendor).toBeNull();
    models.flush({ detail: 'Catalog temporarily unavailable' }, { status: 502, statusText: 'Bad Gateway' });
    fixture.detectChanges();
    expect(element<HTMLInputElement>('provider-model').value).toBe('manual-model-unlisted');
    expect(element<HTMLButtonElement>('provider-save').disabled).toBeFalse();
    expect(component.geminiKeyInput()).toBe('');
    expect(component.ocrKeyInput()).toBe('');
    expect(fixture.nativeElement.textContent).not.toContain(secret);
    flush();
  }));

  it('saves manual IDs through an explicit unverified choice after unsupported catalog failure, with honest status', fakeAsync(() => {
    element('minimax-preset').click();
    fixture.detectChanges();
    tick();
    type('provider-model', 'MiniMax-M3.1-Flash');
    type('provider-key', 'manual-offline-key');
    expect(component.verifyProviderCatalog()).toBeTrue();
    expect(element<HTMLInputElement>('provider-verify-catalog').checked).toBeTrue();
    element('provider-models').click();
    http.expectOne('/api/system/llm-providers/models').flush({ detail: 'Models catalog unsupported' }, { status: 502, statusText: 'Bad Gateway' });
    fixture.detectChanges();
    element('provider-save').click();
    const checkedSave = http.expectOne('/api/system/llm-providers');
    expect(checkedSave.request.body.verify).toBeTrue();
    checkedSave.flush({ detail: 'Catalog returned HTTP 404' }, { status: 400, statusText: 'Bad Request' });
    fixture.detectChanges();
    expect(element<HTMLInputElement>('provider-model').value).toBe('MiniMax-M3.1-Flash');
    expect(element<HTMLInputElement>('provider-key').value).toBe('manual-offline-key');
    element<HTMLInputElement>('provider-verify-catalog').click();
    fixture.detectChanges();
    tick();
    expect(element<HTMLInputElement>('provider-verify-catalog').checked).toBeFalse();
    expect(element('provider-save').textContent).toContain('Save without verification');
    element('provider-save').click();
    const manualSave = http.expectOne('/api/system/llm-providers');
    expect(manualSave.request.body).toEqual(jasmine.objectContaining({
      name: 'minimax', kind: 'anthropic', vendor: 'minimax', model: 'MiniMax-M3.1-Flash',
      api_key: 'manual-offline-key', verify: false
    }));
    http.expectNone('/api/system/llm-providers/models');
    manualSave.flush({ status: 'success' });
    refresh();
    tick();
    fixture.detectChanges();
    expect(component.providerSaveMessage()).toContain('without catalog verification');
    expect(fixture.nativeElement.textContent).toContain('Credentials and model access remain unverified');
    expect(element<HTMLInputElement>('provider-key').value).toBe('');
    expect(service.isCredentialsReady()).toBeFalse();
    expect(service.isSkipCredentialsCheck()).toBeFalse();
    flush();
  }));

  it('shows unavailable data until known MiniMax configuration loads, clears failed data and recovers without invented models', fakeAsync(() => {
    const consoleError = spyOn(console, 'error');
    const minimaxConfig: ModelConfigEnvResponse = {
      ...modelConfig,
      default_model: { provider: 'my-mini', model: 'MiniMax-M3.1-Flash' },
      effective_default_model: {
        provider: 'my-mini', model: 'MiniMax-M3.1-Flash', resolved_via: 'default',
        fallback: { provider: 'my-mini', model: 'MiniMax-M3' }
      }
    };
    service.modelConfigEnv.set(null);
    fixture.detectChanges();
    expect(element('active-model-provider').textContent).toBe('Unavailable');
    expect(element('active-model-name').textContent).toBe('Unavailable');
    expect(component.configuredDefaultLabel()).toBe('Unavailable/Unavailable');
    expect(fixture.nativeElement.querySelector('.model-choice-cards-row .choice-card.selected .choice-desc').textContent)
      .toContain('Custom provider keys use the protected registry; built-in keys can also use .env.');
    expect(fixture.nativeElement.textContent).toContain('Custom LLM Providers (OpenAI / Anthropic Messages)');
    expect(fixture.nativeElement.querySelector('.model-choice-cards-row .tag-badge').textContent).toBe('Built-in provider');

    component.setModelSetupMode('custom');
    http.expectOne('/api/system/model-config-env').flush(minimaxConfig);
    fixture.detectChanges();
    expect(element('active-model-provider').textContent).toBe('my-mini');
    expect(element('active-model-name').textContent).toBe('MiniMax-M3.1-Flash');
    expect(element('configured-model-label').textContent).toBe('my-mini/MiniMax-M3.1-Flash');

    let failedStatus = 0;
    service.fetchModelConfigEnv().subscribe({ error: error => failedStatus = error.status });
    http.expectOne('/api/system/model-config-env').flush({ detail: 'Configuration unavailable' }, { status: 500, statusText: 'Server Error' });
    fixture.detectChanges();
    expect(failedStatus).toBe(500);
    expect(consoleError).toHaveBeenCalled();
    expect(service.modelConfigEnv()).toBeNull();
    expect(element('active-model-provider').textContent).toBe('Unavailable');
    expect(element('active-model-name').textContent).toBe('Unavailable');
    expect(component.configuredDefaultLabel()).toBe('Unavailable/Unavailable');
    expect(fixture.nativeElement.textContent).not.toContain('gemini-3.8-flash');

    component.setModelSetupMode('custom');
    http.expectOne('/api/system/model-config-env').flush(minimaxConfig);
    fixture.detectChanges();
    expect(element('active-model-provider').textContent).toBe('my-mini');
    expect(element('active-model-name').textContent).toBe('MiniMax-M3.1-Flash');
    flush();
  }));

  it('preserves known model fields and translates missing fields when configuration is partial', fakeAsync(() => {
    service.modelConfigEnv.set({
      ...modelConfig, default_model: { provider: 'my-mini' },
      effective_default_model: { provider: 'my-mini', model: '', resolved_via: 'default',
        fallback: { provider: 'my-mini', model: 'MiniMax-M3' } }
    });
    fixture.detectChanges();
    expect(element('active-model-provider').textContent).toBe('my-mini');
    expect(element('active-model-name').textContent).toBe('Unavailable');
    expect(element('configured-model-label').textContent).toBe('my-mini/Unavailable');
    TestBed.inject(I18nService).lang.set('zh');
    fixture.detectChanges();
    expect(element('active-model-provider').textContent).toBe('my-mini');
    expect(element('active-model-name').textContent).toBe('暂无数据');
    expect(element('configured-model-label').textContent).toBe('my-mini/暂无数据');
    flush();
  }));
});
