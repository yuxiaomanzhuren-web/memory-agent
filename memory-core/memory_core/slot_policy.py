"""Read-only capability report. Configuration flags are declarations, not proof."""
import os


def slot_policy(environment=None, startup=None, inference_mode=''):
    env = os.environ if environment is None else environment
    startup = startup or {}
    blockers = ['no_independent_retention_audit']
    if inference_mode == 'dsh':
        blockers.insert(0, 'dsh_session_and_archive')
    if env.get('MEMORY_SLOT_NO_LOG_CONFIRMED') != '1':
        blockers.append('confirmation_disabled')
    return {
        'diagnostic_only': True, 'changes_configuration': False,
        'can_confirm_no_copy': False, 'blockers': blockers,
        'declared_confirmation': env.get('MEMORY_SLOT_NO_LOG_CONFIRMED') == '1',
        'backend_url_configured': bool(env.get('MEMORY_SLOT_LOCAL_URL')),
        'backend_model_configured': bool(env.get('MEMORY_SLOT_LOCAL_MODEL')),
        'startup_status': str(startup.get('status') or 'not_initialized')[:80],
        'startup_phase': str(startup.get('phase') or '')[:80],
        'label_mode_erases_original_body': False,
        'requirements': ['plaintext bypasses DSH sessions and archives',
                         'backend persistence and diagnostics audited',
                         'retry queues, backups and key lifecycle audited',
                         'crash dumps and OS persistence assessed'],
    }