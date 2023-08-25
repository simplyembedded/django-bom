import xmlrpc.client
from django.db.models.signals import post_save, pre_delete
from django.dispatch import receiver, Signal
from .models import Part, PartRevision, ManufacturerPart, PartClass, Subpart, Assembly, AssemblySubparts
from .settings import ODOO_DB, ODOO_COMMON_URL, ODOO_PASSWORD, ODOO_URL, ODOO_USERNAME, ODOO_OBJECT_URL
from .odoo_comm import authenticate_odoo, odoo_search_product_by_name, odoo_get_or_create_all_parent_category, odoo_get_or_create_category_id, odoo_create_or_update_product, get_odoo_product_details, account_for_001_010


@receiver(post_save, sender=Part)
def update_create_part_odoo(sender, instance, **kwargs):   #create/update product
    
    # TODO when a part starting in 001 or 010 gets revised,a whole new part is created in Odoo.maybe this would be beneficial to be able to look at old vers?
    print(instance, 'PART_POST_SAVE\n')
    odoo_product_details = get_odoo_product_details(instance)

    # TODO: For later: Do some exception handling here.
    if odoo_create_or_update_product(odoo_product_details):
        print('Created/updated', instance, '(part) in odoo successfully')
    else:
        print('Unable to authenticate with Odoo,', instance, 'not created/updated')
    
    if odoo_product_details['barcode'][0:3] in ['001', '010']:   # deleting the wrong duplicate because there are 2 creations. May be a better way to do this?
        uid = authenticate_odoo()
        
        if not uid:
            return
        del_barcode = odoo_product_details['barcode']
    
        models = xmlrpc.client.ServerProxy(ODOO_OBJECT_URL)
        product_ids = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'product.template', 'search',
            [[('barcode', '=', del_barcode)]])    
        if product_ids:
            models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'product.template', 'unlink', [[product_ids[0]]])
        
        

@receiver(post_save, sender=PartRevision)                  # revise product
def part_revision_update_odoo(sender, instance, **kwargs):
    print(instance, 'PART_REVISION_POST_SAVE\n')
    odoo_product_details = get_odoo_product_details(instance.part) 


    if odoo_product_details['name'][0:3] in ['001', '010']:     # if the part number begins with 001 or 010, we need to do something different
        new_details = odoo_product_details['name'][:-2] + str(instance.revision)
        odoo_product_details['barcode'] = new_details
        odoo_product_details['internal_reference'] = new_details
        odoo_product_details['part_num'] = new_details
        odoo_product_details['name'] = str(instance.description)
        odoo_product_details['sale_ok'] = True       # Sellable (and purchasable) are true. Purchasable is true by default so no change

    
    if odoo_create_or_update_product(odoo_product_details):
        
        print('Created/updated', instance, '(part rev) in odoo successfully')
        
        
@receiver(pre_delete, sender=PartRevision)    # to delete a part in Odoo if it is deleted in Indabom
def delete_part_in_odoo(sender, instance, **kwargs):
    print('boutta delete', instance)
    
    uid = authenticate_odoo()
    
    if uid:
    
        if str(instance)[0:3] in ['010', '001']:
            details = str(instance).split(',')
            part = details[0][:-2] + details[-1].split()[-1]
            print(part)
        else:
            part = str(instance).split(',')[0]
            print(part)
        
        models = xmlrpc.client.ServerProxy(ODOO_OBJECT_URL)

        if uid:
            product_ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'product.template', 'search', [[('barcode', '=', part)]])
            
            if product_ids:
                product_id = product_ids[0]
                
                boms = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'mrp.bom', 'search', [[]])
                
                if boms:
                    for bom_id in boms:
                        bom = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'mrp.bom', 'read', [[bom_id], ['product_tmpl_id', 'bom_line_ids']])
                        
                        if bom:
                            product_tmpl_id = bom[0]['product_tmpl_id'][0]
                            bom_line_ids = bom[0]['bom_line_ids']
                            
                            # Check if the product is the parent product of the BOM (product_tmpl_id)
                            if product_tmpl_id == product_id:
                                # Delete the entire BOM
                                models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'mrp.bom', 'unlink', [[bom_id]])
                                print(f"Deleted BOM {bom_id} because it's the parent of the product.")
                            else:
                                # Delete BOM lines containing the product
                                for line_id in bom_line_ids:
                                    line = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'mrp.bom.line', 'read', [line_id], {'fields': ['product_id']})
                                    print(line[0]['product_id'][1].split()[0])
                                    if line and line[0]['product_id'][1].split()[0] == '[' + part + ']':
                                        models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'mrp.bom.line', 'unlink', [[line_id]])
                                        print(f"Deleted line {line_id} from BOM {bom_id}")
                
                try:
                    # Delete the product from the product template
                    models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'product.template', 'unlink', [[product_id]])
                    print(f"Deleted product with ID {product_id} from product template.")
                except:
                    print('Product was NOT deleted from product template.')
            else:
                print('Product not found.')
        else:
            print('Authentication failed.')
                    

    
    
@receiver(post_save, sender=PartClass)
def create_class_in_odoo(sender, instance, **kwargs):
    
    new_class = str(instance.code) + ':' + ' ' + str(instance.name)  # creating the new class when created on Indabom admin page. There is a certain format
    
    uid = authenticate_odoo()
    
    if uid:
    
        models = xmlrpc.client.ServerProxy(ODOO_OBJECT_URL)
        parent_category_id = odoo_get_or_create_all_parent_category(models, uid)
        
        odoo_get_or_create_category_id(
        models, uid, new_class, parent_category_id)
    
    else:
        return

    


    


    
    


    
     
        
    
    
    
    



