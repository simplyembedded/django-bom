from .settings import ODOO_DB, ODOO_COMMON_URL, ODOO_PASSWORD, ODOO_URL, ODOO_USERNAME, ODOO_OBJECT_URL
import xmlrpc.client
from django.contrib import messages

def authenticate_odoo():   # call this everytime you need to create or do anything in Odoo using xmlrpc
    
    try:
        common = xmlrpc.client.ServerProxy(ODOO_COMMON_URL)
        if common:
          return common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {})
        else:
            return
    except:
        return 


def odoo_search_product_by_name(barcode, odoo_models, odoo_uid):
    return odoo_models.execute_kw(
        ODOO_DB, odoo_uid, ODOO_PASSWORD,
        'product.template', 'search',
        [[('barcode', '=', barcode)]],
    )

def odoo_get_or_create_all_parent_category(odoo_models, odoo_uid):
    parent_category_id = odoo_models.execute_kw(
        ODOO_DB, odoo_uid, ODOO_PASSWORD,
        'product.category', 
        'search',
        [[('name', '=', 'All')]],
    )

    if not parent_category_id:
        # If the parent category (All) doesn't exist, create a new one
        return odoo_models.execute_kw(
            ODOO_DB, odoo_uid, ODOO_PASSWORD,
            'product.category', 
            'create',
            [{'name': 'All'}]
        )
    return parent_category_id[0]

def odoo_get_or_create_category_id(odoo_models, odoo_uid, category_name, parent_category_id):
    # Search for the child category (like 103: transistor) under the parent category (All)
    category_id = odoo_models.execute_kw(
        ODOO_DB, odoo_uid, ODOO_PASSWORD,
        'product.category', 
        'search',
        [[('name', '=', category_name), ('parent_id', '=', parent_category_id)]],
    )

    if not category_id:
        # If the child category (like 103: transistor) doesn't exist, create a new one under the parent category (All)
        return odoo_models.execute_kw(
            ODOO_DB, odoo_uid, ODOO_PASSWORD,
            'product.category', 
            'create',
            [
                {
                    'name': category_name, 
                    'parent_id': parent_category_id, 
                    'property_cost_method': 'average', 
                    'property_valuation': 'real_time'
                }
            ]
        )
    return category_id[0]

def odoo_create_or_update_product(product_details):
    uid = authenticate_odoo()
    if not uid:
        return False
    
    models = xmlrpc.client.ServerProxy(ODOO_OBJECT_URL)
    product_id = odoo_search_product_by_name(product_details['barcode'], models, uid)
    parent_category_id = odoo_get_or_create_all_parent_category(models, uid)
    category_id = odoo_get_or_create_category_id(
        models, uid, product_details['category_name'], parent_category_id)
        
    if product_id:
        # Product found, update the fields
        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'product.template', 
            'write',
            [
                product_id[0], 
                {
                    'name': product_details['name'],
                    'sale_ok': product_details['sale_ok'],
                    'type': product_details['product_type'],
                    'description': product_details['description'],
                    'description_purchase': product_details['description_purchase'],
                    'categ_id': category_id,
                    'default_code': product_details['internal_reference'],
                    'barcode': product_details['barcode'],
                }
            ]
        )
    else:
        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'product.template', 
            'create',
            [{
                'name': product_details['name'],
                'sale_ok': product_details['sale_ok'],
                'type': product_details['product_type'],
                'description': product_details['description'], 
                'description_purchase': product_details['description_purchase'],
                'categ_id': category_id,
                'default_code': product_details['internal_reference'],
                'barcode': product_details['barcode'],
                'taxes_id': [(6, 0, [11])],           # change tax to GST 5% --> No BC
                'supplier_taxes_id' : [(5, 0, 0)]     # remove 13% HST
            }]
        )

    return True

def get_odoo_product_details(part):
        print('part -->', part)
        if part.primary_manufacturer_part and str(part)[0:3] not in ['010', '001']:
            name = part.primary_manufacturer_part.manufacturer_part_number  # only manufacturer part number (should just be a bunch of numbers)
        else:
            name = part.full_part_number()   # if manufacturer part number and manufacturer are not specified
        category_name = str(part.number_class)
        internal_reference = str(part.number_item)
        part_num = category_name.split(':')[0] + '-' + internal_reference + '-' + str(part.number_variation)
        if part.primary_manufacturer_part:
            description = f'{part.primary_manufacturer_part.manufacturer}<br/>{part.description()}'
            purchase_description = str(part.primary_manufacturer_part.manufacturer) + '\n' + str(part.description())
        else:
            description = part.description()   
            purchase_description = description
        
        return {
            'name': name,
            'category_name': category_name,
            'internal_reference': part_num,
            'part_num': part_num,
            'barcode': part_num,
            'description': description,
            'description_purchase': purchase_description,   
            
            # Default/constant details
            'sale_ok': False,
            'product_type': 'product',
        }


def account_for_001_010(number):  # first part gets the front part, back part gets the rev. this function can't be used if you convert instance to string btw
    return str(number).split(',')[0][:-2] + str(number.latest()).split()[-1]


def add_subparts_to_bom_views(part_number, revision, bom_id, quantity):    # part num in format NNN-NNNNN-NN, which is the barcode
    
    uid = authenticate_odoo()
    models = xmlrpc.client.ServerProxy(ODOO_OBJECT_URL)
    
    if part_number[0:3] in ['001', '010']:
      part_number = part_number[:-2] + revision.split()[-1]
    
    print('expected part number', part_number)
    
    subpart_id = models.execute_kw(     # getting the name of the subpart id from the barcode so we can add it to the parent BOM
        ODOO_DB, uid, ODOO_PASSWORD,
        'product.product', 'search',[[('barcode', '=', part_number)]])
    
    if subpart_id:
        subpart_id = subpart_id[0]
        
        subpart_vals = {
            'product_id': subpart_id,
            'product_qty': quantity    
        }
        
        
        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'mrp.bom', 'write', [[bom_id], {'bom_line_ids': [(0, 0, subpart_vals)]}])

        print("Subpart added successfully to BOM")
    
    else:
        print('Not found')
        return 'False'         # This means that while the BOM was created in Odoo, a subpart was not found so we need to tell the user


def bom_odoo_creation(part_revision, request):

        try:
            bom = part_revision.flat()
        except (RuntimeError, RecursionError):
            messages.error(request, "Error: infinite recursion in part relationship. Contact info@indabom.com to resolve.")
            bom = []
        except AttributeError as err:
            messages.error(request, err)
            bom = []

        # Odoo sync code goes here!
        
        try:
        
            uid = authenticate_odoo()
            models = xmlrpc.client.ServerProxy(ODOO_OBJECT_URL)
            
            
            parent_product = str(part_revision).split(',')[0]
            
            if parent_product[0:3] in ['010', '001']:  
                parent_product = parent_product[:-2] + str(part_revision).split()[-1]
                
            for id, part_bom_item in bom.parts.items():
                part = part_bom_item.part
                part_rev = part_bom_item.part_revision
                qty = part_bom_item.quantity
                
                if (str(part)[:-2] + str(part_rev).split()[-1] == parent_product) or (str(part) == parent_product):
                    odoo_product_details = get_odoo_product_details(part_bom_item.part)


                    if odoo_product_details['name'][0:3] in ['001', '010']:     # if the part number begins with 001 or 010, we need to do something different
                        new_details = parent_product
                        odoo_product_details['barcode'] = new_details
                        odoo_product_details['internal_reference'] = new_details
                        odoo_product_details['part_num'] = new_details
                        odoo_product_details['name'] = str(part_revision.synopsis())
                        odoo_product_details['sale_ok'] = True       # Sellable (and purchasable) are true. Purchasable is true by default so no change

                    odoo_create_or_update_product(odoo_product_details)
                    break
                break
                    
            
            parent_product_id = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 
                                                'product.template', 'search', [[('barcode', '=', parent_product)]])
            if not parent_product_id:
                print(parent_product)
                print('Parent product is not in Odoo')
                return False
                          
            else:
                parent_product_id = parent_product_id[0]      
                
                try:
                    exists_already = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'mrp.bom', 'search', [[('product_tmpl_id', '=', parent_product_id)]])
                    
                    if exists_already:           
                        for id in exists_already:      
                            models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'mrp.bom', 'unlink', [[id]])   
                            print('deleted an outdated BOM')
                except:
                    pass                           
                
                bom_vals = {
                    'product_tmpl_id': parent_product_id,
                    'product_qty': 1.0,  
                }
                
                bom_id = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'mrp.bom', 'create', [bom_vals])
                print('CREATED BOM:', bom_id)
            
            '''
            values accessible through bom variable:
            bom.part_revision   - part_revision the bom is describing  (PartRevision model)
            bom.parts           - parts used in the bom (PartBom obj)
            '''
            for id, part_bom_item in bom.parts.items():
                part = part_bom_item.part
                part_rev = part_bom_item.part_revision
                qty = part_bom_item.quantity
                
                '''
                Available values for reference, remove this when finished!

                Likely only values we'll need:
                part_bom_item.part              - part
                part_bom_item.part_revision     - part_revision
                part_bom_item.quantity          - quantity


                Other values we can use:
                part_bom_item.bom_id            - bom_id
                part_bom_item.extended_quantity - extended_quantity
                part_bom_item._currency         - currency
                part_bom_item.order_cost        - order_cost
                part_bom_item.seller_part       - seller_part
                part_bom_item.alternates        - alternates
                '''
                
                # print(part, part_rev, qty)
                
                if (str(part)[:-2] + str(part_rev).split()[-1] == parent_product) or (str(part) == parent_product): # we already created the parent prod
                    continue
                else:
                    
                 try:
                    odoo_product_details = get_odoo_product_details(part_bom_item.part)
                    
                    # updating/creating subparts that may or may not be in Odoo

                    if odoo_product_details['name'][0:3] in ['001', '010']:     # if the part number begins with 001 or 010, we need to do something different
                        new_details = str(part)[:-2] + str(part_rev).split()[-1]
                        print('new_details', new_details)
                        odoo_product_details['barcode'] = new_details
                        odoo_product_details['internal_reference'] = new_details
                        odoo_product_details['part_num'] = new_details
                        odoo_product_details['name'] = str(part_rev.synopsis())
                        odoo_product_details['sale_ok'] = True      # Sellable (and purchasable) are true. Purchasable is true by default so no change

                    odoo_create_or_update_product(odoo_product_details)
                    add_subparts_to_bom_views(str(part), str(part_rev), bom_id, qty)
                    
                 except:
                     return False
                    
            return True
                 
        except:
            return False

